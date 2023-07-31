import argparse
import gc
import os
import os.path
import re
import json
import shutil
from importlib import reload
from pprint import pprint
import gradio as gr
from modules import (devices, script_callbacks, sd_hijack, sd_models,sd_vae, shared)
import modules.scripts as modules_scripts
from modules.scripts import basedir
from modules.sd_models import checkpoints_loaded
from modules.shared import opts
from modules.sd_samplers import samplers
from modules.ui import create_output_panel, create_refresh_button
import scripts.mergers.mergers
import scripts.mergers.pluslora
import scripts.mergers.xyplot
from importlib import reload
reload(scripts.mergers.mergers)
reload(scripts.mergers.xyplot)
reload(scripts.mergers.pluslora)
import csv
import scripts.mergers.pluslora as pluslora
from scripts.mergers.mergers import (TYPESEG, freezemtime, rwmergelog, simggen,smergegen, blockfromkey)
from scripts.mergers.xyplot import freezetime, nulister, numanager
from scripts.mergers.model_util import filenamecutter

class GenParamGetter(modules_scripts.Script):
    txt2img_gen_button = None
    img2img_gen_button = None

    txt2img_params = []
    img2img_params = []

    def __init__(self) -> None:
        super().__init__()
        script_callbacks.on_app_started(lambda demo, app: self.get_params_components(demo))

    def title(self):
        return "Super Marger Generation Parameter Getter"
    
    def show(self, is_img2img):
        return False

    def after_component(self, component: gr.components.Component, **_kwargs):
        """Find generate button"""
        if component.elem_id == "txt2img_generate":
            GenParamGetter.txt2img_gen_button = component
        elif  component.elem_id == "img2img_generate":
            GenParamGetter.img2img_gen_button = component

    def get_components_by_ids(self, root: gr.Blocks, ids: list[int]):
        components: list[gr.Blocks] = []

        if root._id in ids:
            components.append(root)
            ids = [_id for _id in ids if _id != root._id]

        if isinstance(root, gr.components.BlockContext):
            for block in root.children:
                components.extend(self.get_components_by_ids(block, ids))

        return components
    
    def compare_components_with_ids(self, components: list[gr.Blocks], ids: list[int]):
        return len(components) == len(ids) and all(component._id == _id for component, _id in zip(components, ids))

    def get_params_components(self, demo: gr.Blocks):
        dependencies: list[dict] = [x for x in demo.dependencies if x["trigger"] == "click" and (GenParamGetter.txt2img_gen_button._id if self.is_txt2img else GenParamGetter.img2img_gen_button._id) in x["targets"]]
        dependency: dict = None
        cnet_dependency: dict = None
        UiControlNetUnit = None
        for d in dependencies:
            if len(d["outputs"]) == 1:
                outputs = outputs = self.get_components_by_ids(demo, d["outputs"])
                output = outputs[0]
                if (
                    isinstance(output, gr.State)
                    and type(output.value).__name__ == "UiControlNetUnit"
                ):
                    cnet_dependency = d
                    UiControlNetUnit = type(output.value)

            elif len(d["outputs"]) == 4:
                dependency = d

        params = [params for params in demo.fns if self.compare_components_with_ids(params.inputs, dependency["inputs"])]

        if self.is_txt2img:
            GenParamGetter.txt2img_params = params[0].inputs
        elif self.is_img2img:
            GenParamGetter.txt2img_params = params[0].inputs

path_root = basedir()

def on_ui_tabs():
    weights_presets=""
    userfilepath = os.path.join(path_root, "scripts","mbwpresets.txt")
    if os.path.isfile(userfilepath):
        try:
            with open(userfilepath) as f:
                weights_presets = f.read()
                filepath = userfilepath
        except OSError as e:
                pass
    else:
        filepath = os.path.join(path_root, "scripts","mbwpresets_master.txt")
        try:
            with open(filepath) as f:
                weights_presets = f.read()
                shutil.copyfile(filepath, userfilepath)
        except OSError as e:
                pass

    if "ALLR" not in weights_presets: weights_presets += ADDRAND

    with gr.Blocks() as supermergerui:
        with gr.Tab("Merge"):
            with gr.Row().style(equal_height=False):
                with gr.Column(scale = 3):
                    gr.HTML(value="<p>Merge models and load it for generation</p>")

                    with gr.Row():
                        s_reverse= gr.Button(value="Load settings from:")
                        mergeid = gr.Textbox(label="merged model ID (-1 for last)", elem_id="model_converter_custom_name",value = "-1")

                    with gr.Row():
                        model_a = gr.Dropdown(sd_models.checkpoint_tiles(),elem_id="model_converter_model_name",label="Model A",interactive=True)
                        create_refresh_button(model_a, sd_models.list_models,lambda: {"choices": sd_models.checkpoint_tiles()},"refresh_checkpoint_Z")

                        model_b = gr.Dropdown(sd_models.checkpoint_tiles(),elem_id="model_converter_model_name",label="Model B",interactive=True)
                        create_refresh_button(model_b, sd_models.list_models,lambda: {"choices": sd_models.checkpoint_tiles()},"refresh_checkpoint_Z")

                        model_c = gr.Dropdown(sd_models.checkpoint_tiles(),elem_id="model_converter_model_name",label="Model C",interactive=True)
                        create_refresh_button(model_c, sd_models.list_models,lambda: {"choices": sd_models.checkpoint_tiles()},"refresh_checkpoint_Z")

                    mode = gr.Radio(label = "Merge Mode",choices = ["Weight sum:A*(1-alpha)+B*alpha", "Add difference:A+(B-C)*alpha",
                                                        "Triple sum:A*(1-alpha-beta)+B*alpha+C*beta",
                                                        "sum Twice:(A*(1-alpha)+B*alpha)*(1-beta)+C*beta",
                                                         ], value = "Weight sum:A*(1-alpha)+B*alpha") 
                    calcmode = gr.Radio(label = "Calculation Mode",choices = ["normal", "cosineA", "cosineB","trainDifference","smoothAdd","smoothAdd MT","tensor","tensor2","self"], value = "normal") 
                    with gr.Row(): 
                        useblocks =  gr.Checkbox(label="use MBW")
                        base_alpha = gr.Slider(label="alpha", minimum=-1.0, maximum=2, step=0.001, value=0.5)
                        base_beta = gr.Slider(label="beta", minimum=-1.0, maximum=2, step=0.001, value=0.25)
                        #weights = gr.Textbox(label="weights,base alpha,IN00,IN02,...IN11,M00,OUT00,...,OUT11",lines=2,value="0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5")

                    with gr.Row():
                        with gr.Column(scale = 3):
                            save_sets = gr.CheckboxGroup(["save model", "overwrite","safetensors","fp16","save metadata"], value=["safetensors"], label="save settings")
                        with gr.Column(min_width = 50, scale = 1):
                            id_sets = gr.CheckboxGroup(["image", "PNG info"], label="save merged model ID to")

                    with gr.Row():
                        with gr.Column(min_width = 50):
                            with gr.Row():
                                custom_name = gr.Textbox(label="Custom Name (Optional)", elem_id="model_converter_custom_name")

                        with gr.Column():
                            with gr.Row():
                                bake_in_vae = gr.Dropdown(choices=["None"] + list(sd_vae.vae_dict), value="None", label="Bake in VAE", elem_id="modelmerger_bake_in_vae")
                                create_refresh_button(bake_in_vae, sd_vae.refresh_vae_list, lambda: {"choices": ["None"] + list(sd_vae.vae_dict)}, "modelmerger_refresh_bake_in_vae")


                    with gr.Row():
                        merge = gr.Button(elem_id="model_merger_merge", value="Merge!",variant='primary')
                        mergeandgen = gr.Button(elem_id="model_merger_merge", value="Merge&Gen",variant='primary')
                        gen = gr.Button(elem_id="model_merger_merge", value="Gen",variant='primary')
                        stopmerge = gr.Button(elem_id="stopmerge", value="Stop")

                    with gr.Accordion("Generation Parameters",open = False):
                        gr.HTML(value='If blank or set to 0, parameters in the "txt2img" tab are used.<br>batch size, restore face, hires fix settigns must be set here')
                        prompt = gr.Textbox(label="prompt",lines=1,value="")
                        neg_prompt = gr.Textbox(label="neg_prompt",lines=1,value="")
                        with gr.Row():
                            sampler = gr.Dropdown(label='Sampling method', elem_id=f"sampling", choices=[" ",*[x.name for x in samplers]], value=" ", type="index")
                            steps = gr.Slider(minimum=0.0, maximum=150, step=1, label='Steps',value=0, elem_id="Steps")
                            cfg = gr.Slider(minimum=0.0, maximum=30, step=0.5, label='CFG scale', value=0, elem_id="cfg")
                        with gr.Row():
                            width = gr.Slider(minimum=0, maximum=2048, step=8, label="Width", value=0, elem_id="txt2img_width")
                            height = gr.Slider(minimum=0, maximum=2048, step=8, label="Height", value=0, elem_id="txt2img_height")
                            seed = gr.Number(minimum=-1, maximum=4294967295, step=1, label='Seed', value=0, elem_id="seed")
                        batch_size = denois_str = gr.Slider(minimum=0, maximum=8, step=1, label='Batch size', value=1, elem_id="sm_txt2img_batch_size")
                        genoptions = gr.CheckboxGroup(label = "Gen Options",choices=["Restore faces", "Tiling", "Hires. fix"], visible = True,interactive=True,type="value")    
                        with gr.Row(elem_id="txt2img_hires_fix_row1", variant="compact"):
                            hrupscaler = gr.Dropdown(label="Upscaler", elem_id="txt2img_hr_upscaler", choices=[*shared.latent_upscale_modes, *[x.name for x in shared.sd_upscalers]], value=shared.latent_upscale_default_mode)
                            hr2ndsteps = gr.Slider(minimum=0, maximum=150, step=1, label='Hires steps', value=0, elem_id="txt2img_hires_steps")
                            denois_str = gr.Slider(minimum=0.0, maximum=1.0, step=0.01, label='Denoising strength', value=0.7, elem_id="txt2img_denoising_strength")
                            hr_scale = gr.Slider(minimum=1.0, maximum=4.0, step=0.05, label="Upscale by", value=2.0, elem_id="txt2img_hr_scale")
                        with gr.Row():
                            setdefault = gr.Button(elem_id="setdefault", value="set to default",variant='primary')
                            resetdefault = gr.Button(elem_id="resetdefault", value="reset default",variant='primary')
                            resetcurrent = gr.Button(elem_id="resetcurrent", value="reset current",variant='primary')

                    with gr.Accordion("Elemental Merge, Adjust",open = False):
                        with gr.Row():
                            esettings1 = gr.CheckboxGroup(label = "settings",choices=["print change"],type="value",interactive=True)
                        with gr.Row():
                            deep = gr.Textbox(label="Blocks:Element:Ratio,Blocks:Element:Ratio,...",lines=2,value="")
                        with gr.Row():    
                            tensor = gr.Textbox(label="Adjust(IN,OUT,contrast,colors,colors,colors) 0,0,0,0,0,0,0",lines=2,value="")
                    
                    with gr.Row():
                        x_type = gr.Dropdown(label="X type", choices=[x for x in TYPESEG], value="alpha", type="index")
                        x_randseednum = gr.Number(value=3, label="number of -1", interactive=True, visible = True)
                    xgrid = gr.Textbox(label="Sequential Merge Parameters",lines=3,value="0.25,0.5,0.75")
                    y_type = gr.Dropdown(label="Y type", choices=[y for y in TYPESEG], value="none", type="index")    
                    ygrid = gr.Textbox(label="Y grid (Disabled if blank)",lines=3,value="",visible =False)
                    z_type = gr.Dropdown(label="Z type", choices=[y for y in TYPESEG], value="none", type="index")    
                    zgrid = gr.Textbox(label="Z grid (Disabled if blank)",lines=3,value="",visible =False)
                    esettings = gr.CheckboxGroup(label = "XYZ plot settings",choices=["swap XY","save model","save csv","save anime gif","not save grid","print change"],type="value",interactive=True)
                    with gr.Row():
                        gengrid = gr.Button(elem_id="model_merger_merge", value="Sequential XY Merge and Generation",variant='primary')
                        stopgrid = gr.Button(elem_id="model_merger_merge", value="Stop XY",variant='primary')
                        s_reserve1 = gr.Button(value="Reserve XY Plot",variant='primary')
                    dtrue =  gr.Checkbox(value = True, visible = False)                
                    dfalse =  gr.Checkbox(value = False,visible = False)     
                    dummy_t =  gr.Textbox(value = "",visible = False)    
                blockid=["BASE","IN00","IN01","IN02","IN03","IN04","IN05","IN06","IN07","IN08","IN09","IN10","IN11","M00","OUT00","OUT01","OUT02","OUT03","OUT04","OUT05","OUT06","OUT07","OUT08","OUT09","OUT10","OUT11"]
        
                with gr.Column(scale = 2):
                    currentmodel = gr.Textbox(label="Current Model",lines=1,value="")  
                    submit_result = gr.Textbox(label="Message")
                    mgallery, mgeninfo, mhtmlinfo, mhtmllog = create_output_panel("txt2img", opts.outdir_txt2img_samples)
                    with gr.Accordion("Let the Dice roll",open = False,visible=True):    
                        with gr.Row():
                            gr.HTML(value="<p>R:0~1, U: -0.5~1.5</p>")
                        with gr.Row():
                            luckmode = gr.Radio(label = "Random Mode",choices = ["off", "R", "U", "X", "ER", "EU", "EX","custom"], value = "off") 
                        with gr.Row():
                            lucksets = gr.CheckboxGroup(label = "Settings",choices=["alpha","beta","save E-list"],value=["alpha"],type="value",interactive=True)
                        with gr.Row():
                            luckseed = gr.Number(minimum=-1, maximum=4294967295, step=1, label='Seed for Random Ratio', value=-1, elem_id="luckseed")
                            luckround = gr.Number(minimum=1, maximum=4294967295, step=1, label='Round', value=3, elem_id="luckround")
                            luckserial = gr.Number(minimum=1, maximum=4294967295, step=1, label='Num of challenge', value=1, elem_id="luckchallenge")
                        with gr.Row():  
                            luckcustom = gr.Textbox(label="custom",value = "U,0,0,0,0,0,0,0,0,0,0,0,0,R,R,R,R,R,R,R,R,R,R,R,R,R")
                        with gr.Row():  
                            lucklimits_u = gr.Textbox(label="Upper limit for X",value = "1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1")
                        with gr.Row(): 
                            lucklimits_l = gr.Textbox(label="Lower limit for X",value = "0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0")
                        rand_merge = gr.Button(elem_id="runrandmerge", value="Run Rand",variant='primary')

            with gr.Row(visible = False) as row_inputers:
                inputer = gr.Textbox(label="",lines=1,value="")
                addtox = gr.Button(value="Add to Sequence X")
                addtoy = gr.Button(value="Add to Sequence Y")
            with gr.Row(visible = False) as row_blockids:
                blockids = gr.CheckboxGroup(label = "block IDs",choices=[x for x in blockid],type="value",interactive=True)
            with gr.Row(visible = False) as row_calcmode:
                calcmodes = gr.CheckboxGroup(label = "calcmode",choices=["normal", "cosineA", "cosineB","trainDifference", "smoothAdd","smoothAdd MT","tensor","tensor2","self"],type="value",interactive=True)
            with gr.Row(visible = False) as row_checkpoints:
                checkpoints = gr.CheckboxGroup(label = "checkpoint",choices=[x.model_name for x in sd_models.checkpoints_list.values()],type="value",interactive=True)
            with gr.Row(visible = False) as row_esets:
                pass

            with gr.Tab("Weights Setting"):
                with gr.Row():
                    setalpha = gr.Button(elem_id="copytogen", value="set to alpha",variant='primary')
                    readalpha = gr.Button(elem_id="copytogen", value="read from alpha",variant='primary')
                    setbeta = gr.Button(elem_id="copytogen", value="set to beta",variant='primary')
                    readbeta = gr.Button(elem_id="copytogen", value="read from beta",variant='primary')
                    setx = gr.Button(elem_id="copytogen", value="set to X",variant='primary')
                with gr.Row():
                    weights_a = gr.Textbox(label="weights for alpha: base alpha,IN00,IN02,...IN11,M00,OUT00,...,OUT11",value = "0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5")
                    weights_b = gr.Textbox(label="weights for beta: base beta,IN00,IN02,...IN11,M00,OUT00,...,OUT11",value = "0.2,0.2,0.2,0.2,0.2,0.2,0.2,0.2,0.2,0.2,0.2,0.2,0.2,0.2,0.2,0.2,0.2,0.2,0.2,0.2,0.2,0.2,0.2,0.2,0.2,0.2")
                with gr.Row():
                    with gr.Column():
                        with gr.Row():
                            dd_preset_weight = gr.Dropdown(label="Load preset", choices=preset_name_list(weights_presets), interactive=True, elem_id="refresh_presets")
                            preset_refresh = gr.Button(value='\U0001f504', elem_classes=["tool"])
                            isxl = gr.Radio(label = "type",choices = ["1.X or 2.X", "XL"], value = "1.X or 2.X", type="index") 
                    with gr.Column():
                        with gr.Row():
                            dd_preset_weight_r = gr.Dropdown(label="Load Romdom preset", choices=preset_name_list(weights_presets,True), interactive=True, elem_id="refresh_presets")
                            preset_refresh_r = gr.Button(value='\U0001f504', elem_classes=["tool"])
                            luckab = gr.Radio(label = "for",choices = ["none", "alpha", "beta"], value = "none", type="value") 
                with gr.Row():
                    with gr.Column():
                        base = gr.Slider(label="Base", minimum=0, maximum=1, step=0.0001, value=0.5)
                    with gr.Column():
                        gr.Slider(visible=False)
                    with gr.Column():
                        gr.Slider(visible=False)
                with gr.Row():
                    with gr.Column():
                        in00 = gr.Slider(label="IN00", minimum=0, maximum=1, step=0.0001, value=0.5)
                        in01 = gr.Slider(label="IN01", minimum=0, maximum=1, step=0.0001, value=0.5)
                        in02 = gr.Slider(label="IN02", minimum=0, maximum=1, step=0.0001, value=0.5)
                        in03 = gr.Slider(label="IN03", minimum=0, maximum=1, step=0.0001, value=0.5)
                        in04 = gr.Slider(label="IN04", minimum=0, maximum=1, step=0.0001, value=0.5)
                        in05 = gr.Slider(label="IN05", minimum=0, maximum=1, step=0.0001, value=0.5)
                        in06 = gr.Slider(label="IN06", minimum=0, maximum=1, step=0.0001, value=0.5)
                        in07 = gr.Slider(label="IN07", minimum=0, maximum=1, step=0.0001, value=0.5)
                        in08 = gr.Slider(label="IN08", minimum=0, maximum=1, step=0.0001, value=0.5)
                        in09 = gr.Slider(label="IN09", minimum=0, maximum=1, step=0.0001, value=0.5)
                        in10 = gr.Slider(label="IN10", minimum=0, maximum=1, step=0.0001, value=0.5)
                        in11 = gr.Slider(label="IN11", minimum=0, maximum=1, step=0.0001, value=0.5)
                    with gr.Column():
                        gr.Slider(visible=False)
                        gr.Slider(visible=False)
                        gr.Slider(visible=False)
                        gr.Slider(visible=False)
                        gr.Slider(visible=False)
                        gr.Slider(visible=False)
                        gr.Slider(visible=False)
                        gr.Slider(visible=False)
                        gr.Slider(visible=False)
                        gr.Slider(visible=False)
                        gr.Slider(visible=False)
                        mi00 = gr.Slider(label="M00", minimum=0, maximum=1, step=0.0001, value=0.5, elem_id="supermerger_mbw_M00")
                    with gr.Column():
                        ou11 = gr.Slider(label="OUT11", minimum=0, maximum=1, step=0.0001, value=0.5)
                        ou10 = gr.Slider(label="OUT10", minimum=0, maximum=1, step=0.0001, value=0.5)
                        ou09 = gr.Slider(label="OUT09", minimum=0, maximum=1, step=0.0001, value=0.5)
                        ou08 = gr.Slider(label="OUT08", minimum=0, maximum=1, step=0.0001, value=0.5)
                        ou07 = gr.Slider(label="OUT07", minimum=0, maximum=1, step=0.0001, value=0.5)
                        ou06 = gr.Slider(label="OUT06", minimum=0, maximum=1, step=0.0001, value=0.5)
                        ou05 = gr.Slider(label="OUT05", minimum=0, maximum=1, step=0.0001, value=0.5)
                        ou04 = gr.Slider(label="OUT04", minimum=0, maximum=1, step=0.0001, value=0.5)
                        ou03 = gr.Slider(label="OUT03", minimum=0, maximum=1, step=0.0001, value=0.5)
                        ou02 = gr.Slider(label="OUT02", minimum=0, maximum=1, step=0.0001, value=0.5)
                        ou01 = gr.Slider(label="OUT01", minimum=0, maximum=1, step=0.0001, value=0.5)
                        ou00 = gr.Slider(label="OUT00", minimum=0, maximum=1, step=0.0001, value=0.5)
            with gr.Tab("Weights Presets"):
                with gr.Row():
                    s_reloadtext = gr.Button(value="Reload Presets",variant='primary')
                    s_reloadtags = gr.Button(value="Reload Tags",variant='primary')
                    s_savetext = gr.Button(value="Save Presets",variant='primary')
                    s_openeditor = gr.Button(value="Open TextEditor",variant='primary')
                weightstags= gr.Textbox(label="available",lines = 2,value=tagdicter(weights_presets),visible =True,interactive =True) 
                wpresets= gr.TextArea(label="",value=(weights_presets+ADDRAND),visible =True,interactive  = True)    

            with gr.Tab("Reservation"):
                with gr.Row():
                    s_reserve = gr.Button(value="Reserve XY Plot",variant='primary')
                    s_reloadreserve = gr.Button(value="Reloat List",variant='primary')
                    s_startreserve = gr.Button(value="Start XY plot",variant='primary')
                    s_delreserve = gr.Button(value="Delete list(-1 for all)",variant='primary')
                    s_delnum = gr.Number(value=1, label="Delete num : ", interactive=True, visible = True,precision =0)
                with gr.Row():
                    numaframe = gr.Dataframe(
                        headers=["No.","status","xtype","xmenber","ytype","ymenber","ztype","zmenber","model A","model B","model C","alpha","beta","mode","use MBW","weights alpha","weights beta"],
                        row_count=5,)

            with gr.Row():
                currentcache = gr.Textbox(label="Current Cache")
                loadcachelist = gr.Button(elem_id="model_merger_merge", value="Reload Cache List",variant='primary')
                unloadmodel = gr.Button(value="unload model",variant='primary')


        # main ui end 
    
        with gr.Tab("LoRA", elem_id="tab_lora"):
            pluslora.on_ui_tabs()

                    
        with gr.Tab("Analysis", elem_id="tab_analysis"):
            with gr.Tab("Models"):
                with gr.Row():
                    an_model_a = gr.Dropdown(sd_models.checkpoint_tiles(),elem_id="model_converter_model_name",label="Checkpoint A",interactive=True)
                    create_refresh_button(an_model_a, sd_models.list_models,lambda: {"choices": sd_models.checkpoint_tiles()},"refresh_checkpoint_Z") 
                    an_model_b = gr.Dropdown(sd_models.checkpoint_tiles(),elem_id="model_converter_model_name",label="Checkpoint B",interactive=True)
                    create_refresh_button(an_model_b, sd_models.list_models,lambda: {"choices": sd_models.checkpoint_tiles()},"refresh_checkpoint_Z") 
                with gr.Row():
                    an_mode  = gr.Radio(label = "Analysis Mode",choices = ["ASimilarity","Block","Element","Both"], value = "ASimilarity",type  = "value") 
                    an_calc  = gr.Radio(label = "Block method",choices = ["Mean","Min","attn2"], value = "Mean",type  = "value") 
                    an_include  = gr.CheckboxGroup(label = "Include",choices = ["Textencoder(BASE)","U-Net","VAE"], value = ["Textencoder(BASE)","U-Net"],type  = "value") 
                    an_settings = gr.CheckboxGroup(label = "Settings",choices=["save as txt", "save as csv"],type="value",interactive=True)
                with gr.Row():
                    run_analysis = gr.Button(value="Run Analysis",variant='primary')
                with gr.Row():
                    analysis_cosdif = gr.Dataframe(headers=["block","key","similarity[%]"],)
            with gr.Tab("Text Encoder"):
                    with gr.Row():
                        te_smd_loadkeys = gr.Button(value="Calculate Textencoer",variant='primary')
                        te_smd_searchkeys = gr.Button(value="Search Word(red,blue,girl,...)",variant='primary')
                        exclude = gr.Checkbox(label="exclude non numeric,alphabet,symbol word")
                    pickupword = gr.TextArea()
                    encoded = gr.Dataframe()

        run_analysis.click(fn=calccosinedif,inputs=[an_model_a,an_model_b,an_mode,an_settings,an_include,an_calc],outputs=[analysis_cosdif])    

        with gr.Tab("History", elem_id="tab_history"):
            
            with gr.Row():
                load_history = gr.Button(value="load_history",variant='primary')
                searchwrods = gr.Textbox(label="",lines=1,value="")
                search = gr.Button(value="search")
                searchmode = gr.Radio(label = "Search Mode",choices = ["or","and"], value = "or",type  = "value") 
            with gr.Row():
                history = gr.Dataframe(
                        headers=["ID","Time","Name","Weights alpha","Weights beta","Model A","Model B","Model C","alpha","beta","Mode","use MBW","custum name","save setting","use ID"],
                )
    
        import lora

        with gr.Tab("Elements", elem_id="tab_deep"):
                with gr.Row():
                    smd_model_a = gr.Dropdown(sd_models.checkpoint_tiles(),elem_id="model_converter_model_name",label="Checkpoint A",interactive=True)
                    create_refresh_button(smd_model_a, sd_models.list_models,lambda: {"choices": sd_models.checkpoint_tiles()},"refresh_checkpoint_Z")    
                    smd_loadkeys = gr.Button(value="load keys",variant='primary')
                with gr.Row():
                    smd_lora = gr.Dropdown(list(lora.available_loras.keys()),elem_id="model_converter_model_name",label="Checkpoint A",interactive=True)
                    create_refresh_button(smd_lora, list(lora.available_loras.keys()),lambda: {"choices": list(lora.available_loras.keys())},"refresh_checkpoint_Z")    
                    smd_loadkeys_l = gr.Button(value="load keys",variant='primary')
                with gr.Row():
                    keys = gr.Dataframe(headers=["No.","block","key"],)

        with gr.Tab("Metadeta", elem_id="tab_metadata"):
                with gr.Row():
                    meta_model_a = gr.Dropdown(sd_models.checkpoint_tiles(),elem_id="model_converter_model_name",label="read metadata",interactive=True)
                    create_refresh_button(meta_model_a, sd_models.list_models,lambda: {"choices": sd_models.checkpoint_tiles()},"refresh_checkpoint_Z")    
                    smd_loadmetadata = gr.Button(value="load keys",variant='primary')
                with gr.Row():
                    metadata = gr.TextArea()

        smd_loadmetadata.click(
            fn=loadmetadata,
            inputs=[meta_model_a],
            outputs=[metadata]
        )                 

        smd_loadkeys.click(fn=loadkeys,inputs=[smd_model_a,dfalse],outputs=[keys])
        smd_loadkeys_l.click(fn=loadkeys,inputs=[smd_lora,dtrue],outputs=[keys])

        te_smd_loadkeys.click(fn=encodetexts,inputs=[exclude],outputs=[encoded])
        te_smd_searchkeys.click(fn=pickupencode,inputs=[pickupword],outputs=[encoded])
        

        def unload():
            if shared.sd_model == None: return "already unloaded"
            sd_hijack.model_hijack.undo_hijack(shared.sd_model)
            shared.sd_model = None
            gc.collect()
            devices.torch_gc()
            return "model unloaded"

        unloadmodel.click(fn=unload,outputs=[submit_result])

        load_history.click(fn=load_historyf,outputs=[history ])

        msettings=[weights_a,weights_b,model_a,model_b,model_c,base_alpha,base_beta,mode,calcmode,useblocks,custom_name,save_sets,id_sets,wpresets,deep,tensor,bake_in_vae]
        imagegal = [mgallery,mgeninfo,mhtmlinfo,mhtmllog]
        xysettings=[x_type,xgrid,y_type,ygrid,z_type,zgrid,esettings]
        genparams=[prompt,neg_prompt,steps,sampler,cfg,seed,width,height,batch_size]
        hiresfix = [genoptions,hrupscaler,hr2ndsteps,denois_str,hr_scale]
        lucks = [luckmode,lucksets,lucklimits_u,lucklimits_l,luckseed,luckserial,luckcustom,luckround]

        setdefault.click(fn = configdealer,
            inputs =[*genparams,*hiresfix[1:],dfalse],
        )

        resetdefault.click(fn = configdealer,
            inputs =[*genparams,*hiresfix[1:],dtrue],
        )

        resetcurrent.click(fn = lambda x : [gr.update(value = x) for x in RESETVALS] ,outputs =[*genparams,*hiresfix[1:]],)

        s_reverse.click(fn = reversparams,
            inputs =mergeid,
            outputs = [submit_result,*msettings[0:8],*msettings[9:13],deep,calcmode,luckseed,tensor]
        )

        merge.click(
            fn=smergegen,
            inputs=[*msettings,esettings1,*GenParamGetter.txt2img_params,*genparams,*lucks,currentmodel,dfalse],
            outputs=[submit_result,currentmodel]
        )

        mergeandgen.click(
            fn=smergegen,
            inputs=[*msettings,esettings1,*GenParamGetter.txt2img_params,*genparams,*lucks,currentmodel,dtrue],
            outputs=[submit_result,currentmodel,*imagegal]
        )

        gen.click(
            fn=simggen,
            inputs=[*GenParamGetter.txt2img_params,*genparams,currentmodel,id_sets],
            outputs=[*imagegal],
        )

        s_reserve.click(
            fn=numanager,
            inputs=[gr.Textbox(value="reserve",visible=False),*xysettings,*msettings,*GenParamGetter.txt2img_params,*genparams,*lucks],
            outputs=[numaframe]
        )

        s_reserve1.click(
            fn=numanager,
            inputs=[gr.Textbox(value="reserve",visible=False),*xysettings,*msettings,*GenParamGetter.txt2img_params,*genparams,*lucks],
            outputs=[numaframe]
        )

        gengrid.click(
            fn=numanager,
            inputs=[gr.Textbox(value="normal",visible=False),*xysettings,*msettings,*GenParamGetter.txt2img_params,*genparams,*lucks],
            outputs=[submit_result,currentmodel,*imagegal],
        )

        s_startreserve.click(
            fn=numanager,
            inputs=[gr.Textbox(value=" ",visible=False),*xysettings,*msettings,*GenParamGetter.txt2img_params,*genparams,*lucks],
            outputs=[submit_result,currentmodel,*imagegal],
        )

        rand_merge.click(
            fn=numanager,
            inputs=[gr.Textbox(value="random",visible=False),*xysettings,*msettings,*GenParamGetter.txt2img_params,*genparams,*lucks],
            outputs=[submit_result,currentmodel,*imagegal],
        )

        search.click(fn = searchhistory,inputs=[searchwrods,searchmode],outputs=[history])

        s_reloadreserve.click(fn=nulister,inputs=[dfalse],outputs=[numaframe])
        s_delreserve.click(fn=nulister,inputs=[s_delnum],outputs=[numaframe])
        loadcachelist.click(fn=load_cachelist,inputs=[],outputs=[currentcache])
        addtox.click(fn=lambda x:gr.Textbox.update(value = x),inputs=[inputer],outputs=[xgrid])
        addtoy.click(fn=lambda x:gr.Textbox.update(value = x),inputs=[inputer],outputs=[ygrid])

        stopgrid.click(fn=freezetime)
        stopmerge.click(fn=freezemtime)

        checkpoints.change(fn=lambda x:",".join(x),inputs=[checkpoints],outputs=[inputer])
        blockids.change(fn=lambda x:" ".join(x),inputs=[blockids],outputs=[inputer])
        calcmodes.change(fn=lambda x:",".join(x),inputs=[calcmodes],outputs=[inputer])

        menbers = [base,in00,in01,in02,in03,in04,in05,in06,in07,in08,in09,in10,in11,mi00,ou00,ou01,ou02,ou03,ou04,ou05,ou06,ou07,ou08,ou09,ou10,ou11]

        setalpha.click(fn=slider2text,inputs=[*menbers,wpresets, dd_preset_weight,isxl],outputs=[weights_a])
        setbeta.click(fn=slider2text,inputs=[*menbers,wpresets, dd_preset_weight,isxl],outputs=[weights_b])
        setx.click(fn=add_to_seq,inputs=[xgrid,weights_a],outputs=[xgrid])     

        readalpha.click(fn=text2slider,inputs=weights_a,outputs=menbers)
        readbeta.click(fn=text2slider,inputs=weights_b,outputs=menbers)

        dd_preset_weight.change(fn=on_change_dd_preset_weight,inputs=[wpresets, dd_preset_weight],outputs=menbers)
        dd_preset_weight_r.change(fn=on_change_dd_preset_weight_r,inputs=[wpresets, dd_preset_weight_r,luckab],outputs=[weights_a,weights_b])

        def refresh_presets(presets,rand,ab = ""):
            choices = preset_name_list(presets,rand)
            return gr.update(choices = choices)

        preset_refresh.click(fn=refresh_presets,inputs=[wpresets,dfalse],outputs=[dd_preset_weight])
        preset_refresh_r.click(fn=refresh_presets,inputs=[wpresets,dtrue],outputs=[weights_a,weights_b])

        def changexl(isxl):
            out = [True] * 26
            if isxl:
                for i,id in enumerate(BLOCKID[:-1]):
                    if id not in BLOCKIDXLL[:-1]:
                        out[i] = False
            return [gr.update(visible = x) for x in out]

        isxl.change(fn=changexl,inputs=[isxl], outputs=menbers)

        x_type.change(fn=showxy,inputs=[x_type,y_type,z_type], outputs=[row_blockids,row_checkpoints,row_inputers,ygrid,zgrid,row_esets,row_calcmode])
        y_type.change(fn=showxy,inputs=[x_type,y_type,z_type], outputs=[row_blockids,row_checkpoints,row_inputers,ygrid,zgrid,row_esets,row_calcmode])
        z_type.change(fn=showxy,inputs=[x_type,y_type,z_type], outputs=[row_blockids,row_checkpoints,row_inputers,ygrid,zgrid,row_esets,row_calcmode])
        x_randseednum.change(fn=makerand,inputs=[x_randseednum],outputs=[xgrid])

        import subprocess
        def openeditors():
            subprocess.Popen(['start', filepath], shell=True)

        def reloadpresets():
            try:
                with open(filepath) as f:
                    weights_presets = f.read()
                    choices = preset_name_list(weights_presets)
                    return [weights_presets, gr.update(choices = choices)]
            except OSError as e:
                pass

        def savepresets(text):
            with open(filepath,mode = 'w') as f:
                f.write(text)

        s_reloadtext.click(fn=reloadpresets,inputs=[],outputs=[wpresets, dd_preset_weight])
        s_reloadtags.click(fn=tagdicter,inputs=[wpresets],outputs=[weightstags])
        s_savetext.click(fn=savepresets,inputs=[wpresets],outputs=[])
        s_openeditor.click(fn=openeditors,inputs=[],outputs=[])

    return (supermergerui, "SuperMerger", "supermerger"),

msearch = []
mlist=[]

def loadmetadata(model):
    import json
    checkpoint_info = sd_models.get_closet_checkpoint_match(model)
    if ".safetensors" not in checkpoint_info.filename: return "no metadata(not safetensors)"
    sdict = sd_models.read_metadata_from_safetensors(checkpoint_info.filename)
    if sdict == {}: return "no metadata"
    return json.dumps(sdict,indent=4)

def load_historyf():
    filepath = os.path.join(path_root,"mergehistory.csv")
    global mlist,msearch
    msearch = []
    mlist=[]
    try:
        with  open(filepath, 'r') as f:
            reader = csv.reader(f)
            mlist =  [raw for raw in reader]
            mlist = mlist[1:]
            for m in mlist:
                msearch.append(" ".join(m))
            maxlen = len(mlist[-1][0])
            for i,m in enumerate(mlist):
                mlist[i][0] = mlist[i][0].zfill(maxlen)
            return mlist
    except:
        return [["no data","",""],]

def searchhistory(words,searchmode):
    outs =[]
    ando = "and" in searchmode
    words = words.split(" ") if " " in words else [words]
    for i, m in  enumerate(msearch):
        hit = ando
        for w in words:
            if ando:
                if w not in m:hit = False
            else:
                if w in m:hit = True
        if hit :outs.append(mlist[i])

    if outs == []:return [["no result","",""],]
    return outs

#msettings=[0 weights_a,1 weights_b,2 model_a,3 model_b,4 model_c,5 base_alpha,6 base_beta,7 mode,8 useblocks,9 custom_name,10 save_sets,11 id_sets,12 wpresets]
#13  deep,14 calcmode,15 luckseed
MSETSNUM = 16

def reversparams(id):
    from modules.shared import opts
    def selectfromhash(hash):
        for model in sd_models.checkpoint_tiles():
            if hash in model:
                return model
        return ""
    try:
        idsets = rwmergelog(id = id)
    except:
        return [gr.update(value = "ERROR: history file could not open"),*[gr.update() for x in range(MSETSNUM)]]
    if type(idsets) == str:
        print("ERROR")
        return [gr.update(value = idsets),*[gr.update() for x in range(MSETSNUM)]]
    if idsets[0] == "ID":return  [gr.update(value ="ERROR: no history"),*[gr.update() for x in range(MSETSNUM)]]
    mgs = idsets[3:]
    if mgs[0] == "":mgs[0] = "0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5"
    if mgs[1] == "":mgs[1] = "0.2,0.2,0.2,0.2,0.2,0.2,0.2,0.2,0.2,0.2,0.2,0.2,0.2,0.2,0.2,0.2,0.2,0.2,0.2,0.2,0.2,0.2,0.2,0.2,0.2,0.2"
    mgs[2] = selectfromhash(mgs[2]) if len(mgs[2]) > 5 else ""
    mgs[3] = selectfromhash(mgs[3]) if len(mgs[3]) > 5 else ""
    mgs[4] = selectfromhash(mgs[4]) if len(mgs[4]) > 5 else ""
    mgs[8] = True if mgs[8] =="True" else False
    mgs[10] = mgs[10].replace("[","").replace("]","").replace("'", "") 
    mgs[10] = [x.strip() for x in mgs[10].split(",")]
    mgs[11] = mgs[11].replace("[","").replace("]","").replace("'", "") 
    mgs[11] = [x.strip() for x in mgs[11].split(",")]
    while len(mgs) < MSETSNUM:
        mgs.append("")
    mgs[13] = "normal" if mgs[13] == "" else mgs[13] 
    mgs[14] = -1 if mgs[14] == "" else mgs[14] 
    return [gr.update(value = "setting loaded") ,*[gr.update(value = x) for x in mgs[0:MSETSNUM]]]

def add_to_seq(seq,maker):
    return gr.Textbox.update(value = maker if seq=="" else seq+"\r\n"+maker)

def load_cachelist():
    text = ""
    for x in checkpoints_loaded.keys():
        text = text +"\r\n"+ x.model_name
    return text.replace("\r\n","",1)

def makerand(num):
    text = ""
    for x in range(int(num)):
        text = text +"-1,"
    text = text[:-1]
    return text

#0 row_blockids, 1 row_checkpoints, 2 row_inputers,3 ygrid, 4 zgrid, 5 row_esets, 6 row_calcmode
def showxy(x,y,z):
    flags =[False]*7
    t = TYPESEG
    txy = t[x] + t[y] + t[z]
    if "model" in txy : flags[1] = flags[2] = True
    if "pinpoint" in txy : flags[0] = flags[2] = True
    if "effective" in txy or "element" in txy : flags[5] = True
    if "calcmode" in txy : flags[6] = True
    if not "none" in t[y] : flags[3] = flags[2] = True
    if not "none" in t[z] : flags[4] = flags[2] = True
    return [gr.update(visible = x) for x in flags]

def text2slider(text):
    vals = [t.strip() for t in text.split(",")]
    vals = [0 if v in "RUX" else v for v in vals]
    return [gr.update(value = float(v)) for v in vals]

def slider2text(a,b,c,d,e,f,g,h,i,j,k,l,m,n,o,p,q,r,s,t,u,v,w,x,y,z,presets, preset, isxl):
    az = find_preset_by_name(presets, preset)
    if az is not None:
        if any(element in az for element in RANCHA):return az
    numbers = [a,b,c,d,e,f,g,h,i,j,k,l,m,n,o,p,q,r,s,t,u,v,w,x,y,z]
    if isxl:
        newnums = []
        for i,id in enumerate(BLOCKID[:-1]):
            if id in BLOCKIDXLL[:-1]:
                newnums.append(numbers[i])
        numbers = newnums
    numbers = [str(x) for x in numbers]
    return gr.update(value = ",".join(numbers) )

def on_change_dd_preset_weight(presets, preset):
    weights = find_preset_by_name(presets, preset)
    if weights is not None:
        return text2slider(weights)

def on_change_dd_preset_weight_r(presets, preset, ab):
    weights = find_preset_by_name(presets, preset)
    if weights is not None:
        if "none" in ab : return gr.update(),gr.update()
        if "alpha" in ab : return gr.update(value = weights),gr.update()
        if "beta" in ab : return gr.update(),gr.update(value = weights)
    return gr.update(),gr.update()

RANCHA = ["R","U","X"]

def tagdicter(presets, rand = False):
    presets=presets.splitlines()
    wdict={}
    for l in presets:
        w=""
        if ":" in l :
            key = l.split(":",1)[0]
            w = l.split(":",1)[1]
        if "\t" in l:
            key = l.split("\t",1)[0]
            w = l.split("\t",1)[1]
        if len([w for w in w.split(",")]) == 26:
            if rand and not any(element in w for element in RANCHA) : continue
            wdict[key.strip()]=w
    return ",".join(list(wdict.keys()))

def preset_name_list(presets, rand = False):
    return tagdicter(presets, rand).split(",")

def find_preset_by_name(presets, preset):
    presets = presets.splitlines()
    for l in presets:
        if ":" in l:
            key = l.split(":",1)[0]
            w = l.split(":",1)[1]
        elif "\t" in l:
            key = l.split("\t",1)[0]
            w = l.split("\t",1)[1]
        else:
            continue
        if key == preset and len([w for w in w.split(",")]) == 26:
            return w

    return None

BLOCKID=["BASE","IN00","IN01","IN02","IN03","IN04","IN05","IN06","IN07","IN08","IN09","IN10","IN11","M00","OUT00","OUT01","OUT02","OUT03","OUT04","OUT05","OUT06","OUT07","OUT08","OUT09","OUT10","OUT11","Not Merge"]
BLOCKIDXL=['BASE', 'IN0', 'IN1', 'IN2', 'IN3', 'IN4', 'IN5', 'IN6', 'IN7', 'IN8', 'M', 'OUT0', 'OUT1', 'OUT2', 'OUT3', 'OUT4', 'OUT5', 'OUT6', 'OUT7', 'OUT8', 'VAE']
BLOCKIDXLL=['BASE', 'IN00', 'IN01', 'IN02', 'IN03', 'IN04', 'IN05', 'IN06', 'IN07', 'IN08', 'M00', 'OUT00', 'OUT01', 'OUT02', 'OUT03', 'OUT04', 'OUT05', 'OUT06', 'OUT07', 'OUT08', 'VAE']

def modeltype(sd):
    if "conditioner.embedders.1.model.transformer.resblocks.9.mlp.c_proj.weight" in sd.keys():
        modeltype = "XL"
    else:
        modeltype = "1.X or 2.X"
    return modeltype

def loadkeys(model_a, lora):
    if lora:
        import lora
        sd = sd_models.read_state_dict(lora.available_loras[model_a].filename,"cpu")
    else:
        sd = loadmodel(model_a)
    keys = []
    mtype = modeltype(sd)
    if lora:
        for i, key in enumerate(sd.keys()):
            keys.append([i,"LoRA",key,sd[key].shape])
    else:    
        for i, key in enumerate(sd.keys()):
            keys.append([i,blockfromkey(key,mtype),key,sd[key].shape])

    return keys

def loadmodel(model):
    checkpoint_info = sd_models.get_closet_checkpoint_match(model)
    sd = sd_models.read_state_dict(checkpoint_info.filename,"cpu")
    return sd

from tqdm import tqdm
import torch
from statistics import mean
import csv
import torch.nn as nn
import torch.nn.functional as F

ADDRAND = "\n\
ALL_R	R,R,R,R,R,R,R,R,R,R,R,R,R,R,R,R,R,R,R,R,R,R,R,R,R,R\n\
ALL_U	U,U,U,U,U,U,U,U,U,U,U,U,U,U,U,U,U,U,U,U,U,U,U,U,U,U\n\
ALL_X	X,X,X,X,X,X,X,X,X,X,X,X,X,X,X,X,X,X,X,X,X,X,X,X,X,X\n\
"

def calccosinedif(model_a,model_b,mode,settings,include,calc):
    inc = " ".join(include)
    settings = " ".join(settings)
    a, b = loadmodel(model_a), loadmodel(model_b)
    name = filenamecutter(model_a) + "-" + filenamecutter(model_b)
    cosine_similarities = []
    blocksim = {}
    blockvals = []
    attn2 = {}
    isxl = "XL" == modeltype(a)
    blockids = BLOCKIDXLL if isxl else BLOCKID
    for bl in blockids:
        blocksim[bl] = []
    blocksim["VAE"] = []

    if "ASim" in mode:
        result = asimilarity(a,b,isxl)
        if len(settings) > 1: savecalc(result,name,settings,True,"Asim")
        del a ,b
        gc.collect()
        return result
    else:
        for key in tqdm(a.keys(), desc="Calculating cosine similarity"):
            block = None
            if blockfromkey(key,isxl) == "Not Merge": continue
            if "model_ema" in key: continue
            if "model" not in key:continue
            if "first_stage_model" in key and not ("VAE" in inc):
                continue
            elif "first_stage_model" in key and "VAE" in inc:
                block = "VAE"
            if "diffusion_model" in key and not ("U-Net" in inc): continue
            if "encoder" in key and not ("encoder" in inc): continue
            if key in b and a[key].size() == b[key].size():
                a_flat = a[key].view(-1).to(torch.float32)
                b_flat = b[key].view(-1).to(torch.float32)
                simab = torch.nn.functional.cosine_similarity(a_flat.unsqueeze(0), b_flat.unsqueeze(0))
                if block is None: block,blocks26 = blockfromkey(key,isxl)
                if block =="Not Merge" :continue
                cosine_similarities.append([block, key, round(simab.item()*100,3)])
                blocksim[blocks26].append(round(simab.item()*100,3))
                if "attn2.to_out.0.weight" in key: attn2[block] = round(simab.item()*100,3)

        for bl in blockids:
            val = None
            if bl == "Not Merge": continue
            if bl not in blocksim.keys():continue
            if blocksim[bl] == []: continue
            if "Mean" in calc:
                val = mean(blocksim[bl])
            elif "Min" in calc:
                val = min(blocksim[bl])
            else:
                if bl in attn2.keys():val = attn2[bl]
            if val:blockvals.append([bl,"",round(val,3)])
            if mode != "Element": cosine_similarities.insert(0,[bl,"",round(mean(blocksim[bl]),3)])

        if mode == "Block":
            if len(settings) > 1: savecalc(blockvals,name,settings,True,"Blocks")
            del a ,b
            gc.collect()
            return blockvals
        else:
            if len(settings) > 1: savecalc(cosine_similarities,name,settings,False,"Elements",)
            del a ,b
            gc.collect()
            return cosine_similarities

def savecalc(data,name,settings,blocks,add):
    name = name + "_" + add
    csvpath = os.path.join(path_root,f"{name}.csv")
    txtpath = os.path.join(path_root,f"{name}.txt")

    txt = ""
    for row in data:
        row = [str(r) for r in row]
        txt = txt + ",".join(row)+"\n"
        if blocks: txt = txt.replace(",,",",")

    if "txt" in settings:
        with  open(txtpath, 'w+') as f:
            f.writelines(txt)
            print("file saved to ",txtpath)
    if "csv" in settings:
        with  open(csvpath, 'w+') as f:
            f.writelines(txt)
            print("file saved to ",csvpath)

#code from https://huggingface.co/JosephusCheung/ASimilarityCalculatior

def cal_cross_attn(to_q, to_k, to_v, rand_input):
    hidden_dim, embed_dim = to_q.shape
    attn_to_q = nn.Linear(hidden_dim, embed_dim, bias=False)
    attn_to_k = nn.Linear(hidden_dim, embed_dim, bias=False)
    attn_to_v = nn.Linear(hidden_dim, embed_dim, bias=False)
    attn_to_q.load_state_dict({"weight": to_q})
    attn_to_k.load_state_dict({"weight": to_k})
    attn_to_v.load_state_dict({"weight": to_v})
    
    return torch.einsum(
        "ik, jk -> ik", 
        F.softmax(torch.einsum("ij, kj -> ik", attn_to_q(rand_input), attn_to_k(rand_input)), dim=-1),
        attn_to_v(rand_input)
    )
       
def eval(model, n, input, block):
    qk = f"model.diffusion_model.{block}_block{n}.1.transformer_blocks.0.attn1.to_q.weight"
    uk = f"model.diffusion_model.{block}_block{n}.1.transformer_blocks.0.attn1.to_k.weight"
    vk = f"model.diffusion_model.{block}_block{n}.1.transformer_blocks.0.attn1.to_v.weight"
    atoq, atok, atov = model[qk], model[uk], model[vk]

    attn = cal_cross_attn(atoq, atok, atov, input)
    return attn

ATTN1BLOCKS = [[1,"input"],[2,"input"],[4,"input"],[5,"input"],[7,"input"],[8,"input"],["","middle"],
[3,"output"],[4,"output"],[5,"output"],[6,"output"],[7,"output"],[8,"output"],[9,"output"],[10,"output"],[11,"output"]]

def asimilarity(model_a,model_b,mtype):
    torch.manual_seed(2244096)
    sims = []
  
    for nblock in  tqdm(ATTN1BLOCKS, desc="Calculating cosine similarity"):
        n,block = nblock[0],nblock[1]
        if n != "": n = f"s.{n}"
        key = f"model.diffusion_model.{block}_block{n}.1.transformer_blocks.0.attn1.to_q.weight"

        hidden_dim, embed_dim = model_a[key].shape
        rand_input = torch.randn([embed_dim, hidden_dim])

        attn_a = eval(model_a, n, rand_input, block)
        attn_b = eval(model_b, n, rand_input, block)

        sim = torch.mean(torch.cosine_similarity(attn_a, attn_b))
        sims.append([blockfromkey(key,mtype),"",round(sim.item() * 100,3)])
        
    return sims

CONFIGS = ["prompt","neg_prompt","Steps","Sampling method","CFG scale","Seed","Width","Height","Batch size","Upscaler","Hires steps","Denoising strength","Upscale by"]
RESETVALS = ["","",0," ",0,0,0,0,1,"Latent",0,0.7,2]

def configdealer(prompt,neg_prompt,steps,sampler,cfg,seed,width,height,batch_size,
                        hrupscaler,hr2ndsteps,denois_str,hr_scale,reset):

    data = [prompt,neg_prompt,steps,sampler,cfg,seed,width,height,batch_size,
                        hrupscaler,hr2ndsteps,denois_str,hr_scale]

    current_directory = os.getcwd()
    jsonpath = os.path.join(current_directory,"ui-config.json")
    print(jsonpath)

    with open(jsonpath, 'r') as file:
        json_data = json.load(file)

    for name,men,default in zip(CONFIGS,data,RESETVALS):
        key = f"supermerger/{name}/value"
        json_data[key] = default if reset else men

    with open(jsonpath, 'w') as file:
        json.dump(json_data, file, indent=4)

sorted_output = []

def encodetexts(exclude):
    isxl = hasattr(shared.sd_model,"conditioner")
    model = shared.sd_model.conditioner.embedders[0] if isxl else shared.sd_model.cond_stage_model
    encoder = model.encode_with_transformers
    tokenizer = model.tokenizer
    vocab = tokenizer.get_vocab()

    batch = 500

    b_texts = [list(vocab.items())[i:i + batch] for i in range(0, len(vocab), batch)]

    output = []

    for texts in tqdm(b_texts):    
        batch = []
        words = []
        for word, idx in texts:
            tokens = [model.id_start, idx, model.id_end] + [model.id_end] * 74
            batch.append(tokens)
            words.append((idx, word))
        
        embedding = encoder(torch.IntTensor(batch).to("cuda"))[:,1,:] # (bs,768)
        embedding = embedding.to('cuda')
        emb_norms = torch.linalg.vector_norm(embedding, dim=-1) # (bs,)
        
        for i, (word, token) in enumerate(texts):
            if exclude:
                if has_alphanumeric(word) : output.append([word,token,emb_norms[i].item()])
            else:
                output.append([word,token,emb_norms[i].item()])

    output = sorted(output, key=lambda x: x[2], reverse=True)
    for i in range(len(output)):
        output[i].insert(0,i)

    global sorted_output
    sorted_output = output

    return output[:1000]

def pickupencode(texts):
    wordlist = [x[1] for x in sorted_output]
    texts = texts.split(",")
    output = []
    for text in texts:
        if text in wordlist:
            output.append(sorted_output[wordlist.index(text)])
        if text+"</w>" in wordlist:
            output.append(sorted_output[wordlist.index(text+"</w>")])
    return output

def has_alphanumeric(text):
    pattern = re.compile(r'[a-zA-Z0-9!@#$%^&*()_+{}\[\]:;"\'<>,.?/\|\\]')
    return bool(pattern.search(text.replace("</w>","")))

script_callbacks.on_ui_tabs(on_ui_tabs)
