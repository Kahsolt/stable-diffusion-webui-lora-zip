#!/usr/bin/env python3
# Author: Armit
# Create Time: 2023/10/17

from copy import copy
from pathlib import Path
from typing import List, Dict
from traceback import print_exc

import gradio as gr

import modules.scripts as scripts
from modules.shared import opts
from modules.processing import StableDiffusionProcessing as Processing
from modules.extra_networks import ExtraNetworkParams
from modules.devices import torch_gc

if 'import extensions-builtin/Lora':
    ME_PATH = Path(scripts.basedir())
    BASE_PATH = ME_PATH.parent.parent
    N_EXT_PATH = BASE_PATH / 'extensions-builtin'
    N_EXT_LORA_PATH = N_EXT_PATH / 'Lora'
    import sys ; sys.path.append(str(N_EXT_LORA_PATH))
    from network import Network, NetworkOnDisk
    from network_lora import NetworkModuleLora
    from networks import load_networks, available_networks, loaded_networks
    available_networks: Dict[str, NetworkOnDisk]
    loaded_networks: List[Network]

    AVAILABLE_LORA_NAMES = list(available_networks.keys())

ME_NAME = 'LoRA Zip'
HELP_TEXT = 'Remember to add the L/R LoRAs in your prompt, assure their dims match, otherwise it does NOT work :)'
DEBUG = False

def gr_value(value, visible=True):
    return {'value': value, "visible": visible, "__type__": "update"}

lora_dim: Dict[str, int] = {}

def get_lora_dim(name:str) -> int:
    if name not in lora_dim:
        load_networks([name])
        for net in loaded_networks:
            net: Network
            if net.name == name:
                mod: NetworkModuleLora = list(net.modules.values())[0]
                lora_dim[name] = mod.dim
    return lora_dim[name]

def lora_info(name:str) -> str:
    ver = available_networks[name].sd_version.name
    return f'ver={ver}, dim={get_lora_dim(name)}'


class Script(scripts.Script):

    def title(self):
        return ME_NAME

    def describe(self):
        return 'Zip two LoRA modules dynamically'

    def show(self, is_img2img):
        return scripts.AlwaysVisible

    def ui(self, is_img2img):
        tab = 'i2i' if is_img2img else 't2i'

        with gr.Accordion(label=ME_NAME, open=False, elem_id=f'LZ-{tab}'):
            with gr.Row():
                enable = gr.Checkbox(label='Enable', value=False)

            with gr.Row():
                gr.HTML(value=f'<p style="color:blue"> ↓↓↓ {HELP_TEXT} ↓↓↓ </p>')

            with gr.Row() as tab_lora:
                lora_L = gr.Dropdown(label='Lora left (L)',  choices=AVAILABLE_LORA_NAMES)
                lora_R = gr.Dropdown(label='Lora right (R)', choices=AVAILABLE_LORA_NAMES)

            with gr.Row() as tab_lora_info:
                lora_L_info = gr.HTML(visible=False)
                lora_R_info = gr.HTML(visible=False)

            def _chg_lora(name:str) -> dict:
                return gr_value(lora_info(name) if name in AVAILABLE_LORA_NAMES else '')
            lora_L.change(fn=_chg_lora, inputs=lora_L, outputs=lora_L_info)
            lora_R.change(fn=_chg_lora, inputs=lora_R, outputs=lora_R_info)

            with gr.Row() as tab_w:
                w = gr.Slider(label='Up/Down weight',    minimum=0.0, maximum=1.0, step=0.01, value=lambda: 1.0)
                v = gr.Slider(label='Multiplier weight', minimum=0.0, maximum=1.0, step=0.01, value=lambda: 0.5)

            with gr.Row() as tab_w_info:
                gr.HTML('<span> up = w * uL + (1 - w) * uR ; down = (1 - w) * dL + w * dR </span>')
                gr.HTML('<span> mult = w * L + (1 - w) * R </span>')

        return [
            enable,
            lora_L, lora_R, w, v,
        ]

    def after_extra_networks_activate(self, p:Processing, 
            enable:bool,
            lora_L:str, lora_R:str, w:float, v:float,
            **kwargs
        ):
        if not enable: return

        extra_network_data = kwargs.get('extra_network_data')
        if 'lora' not in extra_network_data: return
        lora_params: List[ExtraNetworkParams] = extra_network_data['lora']

        # NOTE: this hook `after_extra_networks_activate` is called at end of the function `activate`
        # which is called per batch iter in `process_images_inner`, between hook `before_process_batch` and `process_batch`
        from modules.extra_networks import activate
        assert activate
        from modules.processing import process_images_inner
        assert process_images_inner
        # the `activate` will load lora data and update `Lora.networks.loaded_networks` list
        assert loaded_networks

        lora_activated: List[str] = []
        for lora in lora_params:
            name, *args = lora.items
            lora_activated.append(name)
        if DEBUG: print(f'>> LoRA acts: {lora_activated}')
        if len(lora_activated) < 2:
            print(f'>> should apply at least 2 LoRAs, but got {len(lora_activated)}: {lora_activated}')
            return

        lora_missing = {lora_L, lora_R} - set(lora_activated)
        if lora_missing:
            print(f'>> some LoRAs not activated in your prompts: {lora_missing}')
            return

        if not lora_L: lora_L = lora_activated[0]
        if not lora_R: lora_R = lora_activated[1]
        print('lora_L:', lora_L)
        print('lora_R:', lora_R)

        dim_L = get_lora_dim(lora_L)
        dim_R = get_lora_dim(lora_R)
        if dim_L != dim_R:
            print(f'>> LoRAs dim mismatch: L({dim_L}) != R({dim_R})')
            return

        lora_loaded: List[str] = []
        lora_nets: Dict[str, Network] = {}
        for net in loaded_networks:
            name = net.name
            lora_loaded.append(name)
            if name not in {lora_L, lora_R}: continue
            lora_nets[name] = net
        if DEBUG: print(f'>> LoRA loads: {lora_loaded}')

        lora_L_net = lora_nets[lora_L]
        lora_R_net = lora_nets[lora_R]
        lora_L_keys = lora_L_net.modules.keys()
        lora_R_keys = lora_R_net.modules.keys()
        if set(lora_L_keys) != set(lora_R_keys):
            print(f'>> LoRAs structure mismatch, len(lora_L)={len(lora_L_keys)} != len(lora_R)={len(lora_R_keys)}')
            return

        # pack args
        self.lora_L = lora_L
        self.lora_R = lora_R
        self.w = w

        # make the fused tmp lora
        try:
            lora_M_net = self.fuse_lora(lora_L_net, lora_R_net, w, v)
            print('>> fuse lora success')
        except:
            print_exc()
            return
        finally:
            torch_gc()

        # load the fused tmp LoRA
        self.load_fused_lora(p, lora_M_net)

        # unload the LR LoRA
        self.unload_lr_loras(p)

    # ↓↓↓ utils ↓↓↓
    def fuse_lora(self, L:Network, R:Network, w:float, v:float) -> Network:
        ''' fuse L/R LoRAs to create a new tmp LoRA '''

        M = Network(ME_NAME, None)
        M.  te_multiplier = v * L.  te_multiplier + (1 - v) * R.te_multiplier
        M.unet_multiplier = v * L.unet_multiplier + (1 - v) * R.unet_multiplier
        M.dyn_dim = v * (getattr(L, 'dyn_dim') or 1.0) + (1 - v) * (getattr(R, 'dyn_dim') or 1.0)
        M.mtime = max(L.mtime, R.mtime)
        M.mentioned_name = 'LoRA zip'

        for key in L.modules.keys():
            mod_L: NetworkModuleLora = L.modules[key]
            mod_R: NetworkModuleLora = R.modules[key]

            # NOTE: up_model/down_model has no bias
            #   up.shape: [D=3072/768, R=16]
            # down.shape: [R=16, D=768/3072]
            up   =      w  * mod_L.  up_model.weight + (1 - w) * mod_R.  up_model.weight
            down = (1 - w) * mod_L.down_model.weight +      w  * mod_R.down_model.weight

            mod_M: NetworkModuleLora = copy(mod_L)
            mod_M.  up_model.weight.data = up
            mod_M.down_model.weight.data = down
            M.modules[key] = mod_M

        return M

    def load_fused_lora(self, p:Processing, net:Network):
        ''' insert the fused LoRA to activated & loaded '''

        lora_params: List[ExtraNetworkParams] = p.extra_network_data['lora']
        lora_param = ExtraNetworkParams(items=[ME_NAME, opts.extra_networks_default_multiplier])
        lora_params.append(lora_param)

        loaded_networks.append(net)

    def unload_lr_loras(self, p:Processing):
        ''' kick out the L/R LoRAs from activated & loaded '''

        names = [self.lora_L, self.lora_R]
        lora_params: List[ExtraNetworkParams] = p.extra_network_data['lora']

        to_remove = []
        for lora in lora_params:
            name, *args = lora.items
            if name in names:
                to_remove.append(lora)
        for lora in to_remove:
            lora_params.remove(lora)

        to_remove = []
        for net in loaded_networks:
            if net.name in names:
                to_remove.append(net)
        for net in to_remove:
            loaded_networks.remove(net)
