# Copyright 2024 Huawei Technologies Co., Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
""" Llava model configuration"""

import warnings

from ...configuration_utils import PretrainedConfig
from ....utils import logging
from ..auto import CONFIG_MAPPING


logger = logging.get_logger(__name__)


class LlavaConfig(PretrainedConfig):
    r"""
    This is the configuration class to store the configuration of a [`LlavaForConditionalGeneration`]. It is used to instantiate an
    Llava model according to the specified arguments, defining the model architecture. Instantiating a configuration
    with the defaults will yield a similar configuration to that of the Llava-9B.

    e.g. [llava-hf/llava-9b](https://huggingface.co/llava-hf/llava-9b)

    Configuration objects inherit from [`PretrainedConfig`] and can be used to control the model outputs. Read the
    documentation from [`PretrainedConfig`] for more information.

    Args:
        vision_config (`Union[AutoConfig, dict]`,  *optional*, defaults to `CLIPVisionConfig`):
            The config object or dictionary of the vision backbone.
        text_config (`Union[AutoConfig, dict]`, *optional*, defaults to `LlamaConfig`):
            The config object or dictionary of the text backbone.
        ignore_index (`int`, *optional*, defaults to -100):
            The ignore index for the loss function.
        image_token_index (`int`, *optional*, defaults to 32000):
            The image token index to encode the image prompt.
        projector_hidden_act (`str`, *optional*, defaults to `"gelu"`):
            The activation function used by the multimodal projector.
        vision_feature_select_strategy (`str`, *optional*, defaults to `"default"`):
            The feature selection strategy used to select the vision feature from the vision backbone.
            Can be one of `"default"` or `"full"`.
        vision_feature_layer (`int`, *optional*, defaults to -2):
            The index of the layer to select the vision feature.

    Example:
        ```python
        >>> from transformers import LlavaForConditionalGeneration, LlavaConfig, CLIPVisionConfig, LlamaConfig
        ...
        >>> # Initializing a CLIP-vision config
        >>> vision_config = CLIPVisionConfig()
        ...
        >>> # Initializing a Llama config
        >>> text_config = LlamaConfig()
        ...
        >>> # Initializing a Llava llava-1.5-7b style configuration
        >>> configuration = LlavaConfig(vision_config, text_config)
        ...
        >>> # Initializing a model from the llava-1.5-7b style configuration
        >>> model = LlavaForConditionalGeneration(configuration)
        ...
        >>> # Accessing the model configuration
        >>> configuration = model.config
        ```
    """
    model_type = "llava"
    is_composition = False

    def __init__(
        self,
        vision_config=None,
        text_config=None,
        ignore_index=-100,
        image_token_index=32000,
        projector_hidden_act="gelu",
        vision_feature_select_strategy="default",
        vision_feature_layer=-2,
        **kwargs,
    ):
        """
        Initializes an instance of the LlavaConfig class.

        Args:
            self: The instance of the class.
            vision_config (dict or None): Configuration options for the vision model.
                If provided as a dictionary, must include the 'model_type' key. Default is None.
            text_config (dict or None): Configuration options for the text model.
                If provided as a dictionary, must include the 'model_type' key. Default is None.
            ignore_index (int): The index to ignore during computations. Default is -100.
            image_token_index (int): The index assigned to image tokens. Default is 32000.
            projector_hidden_act (str): The activation function for the projector. Default is 'gelu'.
            vision_feature_select_strategy (str): The strategy to select vision features.
                Valid values are 'default' and 'full'. Default is 'default'.
            vision_feature_layer (int): The layer to extract vision features from. Default is -2.
            **kwargs: Additional keyword arguments.

        Returns:
            None.

        Raises:
            ValueError: If the provided vision_feature_select_strategy is not 'default' or 'full'.
            FutureWarning: If the 'vocab_size' argument is deprecated and no longer has any effect.

        Note:
            - The 'vision_config' parameter can be provided as a dictionary or None. If a dictionary is provided, it must include the 'model_type' key.
            - If 'vision_config' is None, a default configuration is used.
            - The 'text_config' parameter can be provided as a dictionary or None. If a dictionary is provided, it must include the 'model_type' key.
            - If 'text_config' is None, a default configuration is used.
            - The '_vocab_size' attribute is set based on the 'text_config' vocabulary size.
        """
        self.ignore_index = ignore_index
        self.image_token_index = image_token_index
        self.projector_hidden_act = projector_hidden_act

        if vision_feature_select_strategy not in ["default", "full"]:
            raise ValueError(
                "vision_feature_select_strategy should be one of 'default', 'full'."
                f"Got: {vision_feature_select_strategy}"
            )

        if "vocab_size" in kwargs:
            warnings.warn(
                "The `vocab_size` argument is deprecated and will be removed in v4.42, since it can be inferred from the `text_config`. Passing this argument has no effect",
                FutureWarning,
            )

        self.vision_feature_select_strategy = vision_feature_select_strategy
        self.vision_feature_layer = vision_feature_layer

        if isinstance(vision_config, dict):
            vision_config["model_type"] = (
                vision_config["model_type"] if "model_type" in vision_config else "clip_vision_model"
            )
            vision_config = CONFIG_MAPPING[vision_config["model_type"]](
                **vision_config)
        elif vision_config is None:
            vision_config = CONFIG_MAPPING["clip_vision_model"](
                intermediate_size=4096,
                hidden_size=1024,
                patch_size=14,
                image_size=336,
                num_hidden_layers=24,
                num_attention_heads=16,
                vocab_size=32000,
                projection_dim=768,
            )

        self.vision_config = vision_config

        if isinstance(text_config, dict):
            text_config["model_type"] = text_config["model_type"] if "model_type" in text_config else "llama"
            text_config = CONFIG_MAPPING[text_config["model_type"]](
                **text_config)
        elif text_config is None:
            text_config = CONFIG_MAPPING["llama"]()

        self.text_config = text_config
        self._vocab_size = self.text_config.vocab_size

        super().__init__(**kwargs)

    @property
    def vocab_size(self):
        """
        Method to retrieve the vocabulary size.

        Args:
            self (LlavaConfig): The instance of the LlavaConfig class.
                This parameter refers to the current instance of the LlavaConfig class.
                It is used to access the internal attributes and configurations of the class.

        Returns:
            None.

        Raises:
            FutureWarning: This method raises a FutureWarning when accessed,
                indicating that the 'vocab_size' attribute is deprecated. Users are advised to use
                'text_config.vocab_size' instead.
        """
        warnings.warn(
            "The `vocab_size` attribute is deprecated and will be removed in v4.42, Please use `text_config.vocab_size` instead.",
            FutureWarning,
        )
        return self._vocab_size

    @vocab_size.setter
    def vocab_size(self, value):
        """
        Sets the vocabulary size for the LlavaConfig class.

        Args:
            self (LlavaConfig): The instance of the LlavaConfig class.
            value (int): The new vocabulary size to be set. It should be a positive integer.

        Returns:
            None.

        Raises:
            None.

        This method is used to set the vocabulary size for the LlavaConfig class. The vocabulary size determines
        the number of unique words that can be stored in the vocabulary. It is important to set an appropriate
        vocabulary size based on the application and the amount of available memory. The vocabulary size can only be
        set to a positive integer value, otherwise an error will be raised.

        Example:
            ```python
           >>> config = LlavaConfig()
           >>> config.vocab_size = 10000
            ```
        """
        self._vocab_size = value

    def to_dict(self):
        """
        Converts the LlavaConfig object into a dictionary representation.
        
        Args:
            self (LlavaConfig): The LlavaConfig object itself.
        
        Returns:
            dict: A dictionary representation of the LlavaConfig object, excluding the '_vocab_size' attribute.
        
        Raises:
            None
        
        Note:
            This method is inherited from the parent class and modified to exclude the '_vocab_size' attribute from the output dictionary.
        """
        output = super().to_dict()
        output.pop("_vocab_size", None)
        return output


__all__ = [
    "LlavaConfig",
]
