from typing import List, Dict

import torch
from loguru import logger
import numpy as np

from iopaint.download import scan_models
from iopaint.helper import switch_mps_device
from iopaint.model import models, ControlNet, SD, SDXL
from iopaint.model.utils import torch_gc
from iopaint.model_info import ModelInfo, ModelType
from iopaint.schema import InpaintRequest


class ModelManager:
    def __init__(self, name: str, device: torch.device, **kwargs):
        self.name = name
        self.device = device
        self.kwargs = kwargs
        self.available_models: Dict[str, ModelInfo] = {}
        self.scan_models()

        self.enable_controlnet = kwargs.get("enable_controlnet", False)
        controlnet_method = kwargs.get("controlnet_method", None)
        if (
            controlnet_method is None
            and name in self.available_models
            and self.available_models[name].support_controlnet
        ):
            controlnet_method = self.available_models[name].controlnets[0]
        self.controlnet_method = controlnet_method
        self.model = self.init_model(name, device, **kwargs)

    @property
    def current_model(self) -> ModelInfo:
        return self.available_models[self.name]

    def init_model(self, name: str, device, **kwargs):
        logger.info(f"Loading model: {name}")
        if name not in self.available_models:
            raise NotImplementedError(
                f"Unsupported model: {name}. Available models: {self.available_models.keys()}"
            )

        model_info = self.available_models[name]
        kwargs = {
            **kwargs,
            "model_info": model_info,
            "enable_controlnet": self.enable_controlnet,
            "controlnet_method": self.controlnet_method,
        }

        if model_info.support_controlnet and self.enable_controlnet:
            return ControlNet(device, **kwargs)
        elif model_info.name in models:
            return models[name](device, **kwargs)
        else:
            if model_info.model_type in [
                ModelType.DIFFUSERS_SD_INPAINT,
                ModelType.DIFFUSERS_SD,
            ]:
                return SD(device, **kwargs)

            if model_info.model_type in [
                ModelType.DIFFUSERS_SDXL_INPAINT,
                ModelType.DIFFUSERS_SDXL,
            ]:
                return SDXL(device, **kwargs)

        raise NotImplementedError(f"Unsupported model: {name}")

    def __call__(self, image, mask, config: InpaintRequest):
        """

        Args:
            image: [H, W, C] RGB
            mask: [H, W, 1] 255 means area to repaint
            config:

        Returns:
            BGR image
        """
        self.switch_controlnet_method(config)
        self.enable_disable_freeu(config)
        self.enable_disable_lcm_lora(config)
        return self.model(image, mask, config).astype(np.uint8)

    def scan_models(self) -> List[ModelInfo]:
        available_models = scan_models()
        self.available_models = {it.name: it for it in available_models}
        return available_models

    def switch(self, new_name: str):
        if new_name == self.name:
            return

        old_name = self.name
        old_controlnet_method = self.controlnet_method
        self.name = new_name

        if (
            self.available_models[new_name].support_controlnet
            and self.controlnet_method
            not in self.available_models[new_name].controlnets
        ):
            self.controlnet_method = self.available_models[new_name].controlnets[0]
        try:
            # TODO: enable/disable controlnet without reload model
            del self.model
            torch_gc()

            self.model = self.init_model(
                new_name, switch_mps_device(new_name, self.device), **self.kwargs
            )
        except Exception as e:
            self.name = old_name
            self.controlnet_method = old_controlnet_method
            logger.info(f"Switch model from {old_name} to {new_name} failed, rollback")
            self.model = self.init_model(
                old_name, switch_mps_device(old_name, self.device), **self.kwargs
            )
            raise e

    def switch_controlnet_method(self, config):
        if not self.available_models[self.name].support_controlnet:
            return

        if (
            self.enable_controlnet
            and config.controlnet_method
            and self.controlnet_method != config.controlnet_method
        ):
            old_controlnet_method = self.controlnet_method
            self.controlnet_method = config.controlnet_method
            self.model.switch_controlnet_method(config.controlnet_method)
            logger.info(
                f"Switch Controlnet method from {old_controlnet_method} to {config.controlnet_method}"
            )
        elif self.enable_controlnet != config.enable_controlnet:
            self.enable_controlnet = config.enable_controlnet
            self.controlnet_method = config.controlnet_method

            self.model = self.init_model(
                self.name, switch_mps_device(self.name, self.device), **self.kwargs
            )
            if not config.enable_controlnet:
                logger.info(f"Disable controlnet")
            else:
                logger.info(f"Enable controlnet: {config.controlnet_method}")

    def enable_disable_freeu(self, config: InpaintRequest):
        if str(self.model.device) == "mps":
            return

        if self.available_models[self.name].support_freeu:
            if config.sd_freeu:
                freeu_config = config.sd_freeu_config
                self.model.model.enable_freeu(
                    s1=freeu_config.s1,
                    s2=freeu_config.s2,
                    b1=freeu_config.b1,
                    b2=freeu_config.b2,
                )
            else:
                self.model.model.disable_freeu()

    def enable_disable_lcm_lora(self, config: InpaintRequest):
        if self.available_models[self.name].support_lcm_lora:
            if config.sd_lcm_lora:
                if not self.model.model.get_list_adapters():
                    self.model.model.load_lora_weights(self.model.lcm_lora_id)
            else:
                self.model.model.disable_lora()
