# coding=utf-8
# Copyright 2024 Meta AI and The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Feature extractor class for Musicgen Melody
"""
import copy
from typing import Any, Dict, List, Optional, Union

import numpy as np
from mindspore.dataset.audio import ResampleMethod, Resample, Spectrogram

from ...audio_utils import chroma_filter_bank
from ...feature_extraction_sequence_utils import SequenceFeatureExtractor
from ...feature_extraction_utils import BatchFeature
from ....utils import TensorType, is_mindspore_available, logging


if is_mindspore_available():
    import mindspore
    from mindnlp.core import ops, nn

logger = logging.get_logger(__name__)


class MusicgenMelodyFeatureExtractor(SequenceFeatureExtractor):
    r"""
    Constructs a MusicgenMelody feature extractor.

    This feature extractor inherits from [`~feature_extraction_sequence_utils.SequenceFeatureExtractor`] which contains
    most of the main methods. Users should refer to this superclass for more information regarding those methods.

    This class extracts chroma features from audio processed by [Demucs](https://github.com/adefossez/demucs/tree/main) or
    directly from raw audio waveform.

    Args:
        feature_size (`int`, *optional*, defaults to 12):
            The feature dimension of the extracted features.
        sampling_rate (`int`, *optional*, defaults to 32000):
            The sampling rate at which the audio files should be digitalized expressed in hertz (Hz).
        hop_length (`int`, *optional*, defaults to 4096):
            Length of the overlaping windows for the STFT used to obtain the Mel Frequency coefficients.
        chunk_length (`int`, *optional*, defaults to 30):
            The maximum number of chunks of `sampling_rate` samples used to trim and pad longer or shorter audio
            sequences.
        n_fft (`int`, *optional*, defaults to 16384):
            Size of the Fourier transform.
        num_chroma (`int`, *optional*, defaults to 12):
            Number of chroma bins to use.
        padding_value (`float`, *optional*, defaults to 0.0):
            Padding value used to pad the audio.
        return_attention_mask (`bool`, *optional*, defaults to `False`):
            Whether to return the attention mask. Can be overwritten when calling the feature extractor.

            [What are attention masks?](../glossary#attention-mask)

            <Tip>

            For Whisper models, `attention_mask` should always be passed for batched inference, to avoid subtle
            bugs.

            </Tip>
        stem_indices (`List[int]`, *optional*, defaults to `[3, 2]`):
            Stem channels to extract if demucs outputs are passed.
    """
    model_input_names = ["input_features"]

    def __init__(
        self,
        feature_size=12,
        sampling_rate=32000,
        hop_length=4096,
        chunk_length=30,
        n_fft=16384,
        num_chroma=12,
        padding_value=0.0,
        return_attention_mask=False,  # pad inputs to max length with silence token (zero) and no attention mask
        stem_indices=[3, 2],
        **kwargs,
    ):
        """
        Initializes a MusicgenMelodyFeatureExtractor object.
        
        Args:
            self: The object itself.
            feature_size (int, optional): The size of the feature. Defaults to 12.
            sampling_rate (int, optional): The sampling rate. Defaults to 32000.
            hop_length (int, optional): The hop length. Defaults to 4096.
            chunk_length (int, optional): The length of the chunk. Defaults to 30.
            n_fft (int, optional): The number of FFT points. Defaults to 16384.
            num_chroma (int, optional): The number of chroma. Defaults to 12.
            padding_value (float, optional): The padding value. Defaults to 0.0.
            return_attention_mask (bool, optional): Indicates whether to return attention mask. Defaults to False.
            stem_indices (list, optional): The stem indices. Defaults to [3, 2].
            **kwargs: Additional keyword arguments.
        
        Returns:
            None.
        
        Raises:
            ValueError: If the provided feature size, sampling rate, hop length, chunk length, n_fft, or num_chroma
                is invalid.
            TypeError: If the provided padding value is not a float.
            TypeError: If the provided return_attention_mask is not a boolean.
            TypeError: If stem_indices is not a list.
        """
        super().__init__(
            feature_size=feature_size,
            sampling_rate=sampling_rate,
            padding_value=padding_value,
            return_attention_mask=return_attention_mask,
            **kwargs,
        )
        self.n_fft = n_fft
        self.hop_length = hop_length
        self.chunk_length = chunk_length
        self.n_samples = chunk_length * sampling_rate
        self.sampling_rate = sampling_rate
        self.chroma_filters = mindspore.Tensor.from_numpy(
            chroma_filter_bank(sampling_rate=sampling_rate, num_frequency_bins=n_fft, tuning=0, num_chroma=num_chroma)
        ).float()
        self.spectrogram = Spectrogram(n_fft=n_fft, win_length=n_fft, hop_length=hop_length, power=2, center=True, pad=0, normalized=True)

        self.stem_indices = stem_indices

    def _ms_extract_fbank_features(self, waveform: mindspore.Tensor) -> mindspore.Tensor:
        """
        Compute the chroma spectrogram of the provided audio using the spectrogram implementation and the librosa chroma features.
        """
        # if wav length is not long enough, pad it
        wav_length = waveform.shape[-1]
        if wav_length < self.n_fft:
            pad = self.n_fft - wav_length
            rest = 0 if pad % 2 == 0 else 1
            waveform = ops.pad(waveform, (pad // 2, pad // 2 + rest), "constant", 0)

        # squeeze alongside channel dimension
        spec = mindspore.tensor(self.spectrogram(waveform.asnumpy()))

        # sum along the frequency dimension
        raw_chroma = ops.einsum("cf, ...ft->...ct", self.chroma_filters, spec)

        # normalise with max value
        norm_chroma = nn.functional.normalize(raw_chroma, p=float("inf"), dim=-2, eps=1e-6)

        # transpose time and chroma dimension -> (batch, time, chroma)
        norm_chroma = norm_chroma.swapaxes(1, 2)

        # replace max value alongside chroma dimension with 1 and replace the rest with 0
        idx = norm_chroma.argmax(-1, keepdims=True)
        norm_chroma[:] = 0
        norm_chroma = ops.scatter(norm_chroma, -1, idx, ops.ones_like(idx, dtype=norm_chroma.dtype))

        return norm_chroma

    def _extract_stem_indices(self, audio, sampling_rate=None):
        """
        Extracts stems from the output of the [Demucs](https://github.com/adefossez/demucs/tree/main) audio separation
        model, then converts to mono-channel and resample to the feature extractor sampling rate.

        Args:
            audio (`mindspore.Tensor` of shape `(batch_size, num_stems, channel_size, audio_length)`):
                The output of the Demucs model to be processed.
            sampling_rate (`int`, *optional*):
                Demucs sampling rate. If not specified, defaults to `44000`.
        """
        sampling_rate = 44000 if sampling_rate is None else sampling_rate

        # extract "vocals" and "others" sources from audio encoder (demucs) output
        # [batch_size, num_stems, channel_size, audio_length]
        wav = audio[:, mindspore.tensor(self.stem_indices)]

        # merge extracted stems to single waveform
        wav = wav.sum(1)

        # convert to mono-channel waveform
        wav = wav.mean(axis=1, keep_dims=True)

        # resample to model sampling rate
        # not equivalent to julius.resample
        if sampling_rate != self.sampling_rate:

            transform = Resample(sampling_rate, self.sampling_rate,
                             resample_method=ResampleMethod.SINC_INTERPOLATION,
                             rolloff=0.945, lowpass_filter_width=24)
            wav = mindspore.Tensor(transform(wav.asnumpy()))

        # [batch_size, 1, audio_length] -> [batch_size, audio_length]
        wav = wav.squeeze(1)

        return wav

    def __call__(
        self,
        audio: Union[np.ndarray, List[float], List[np.ndarray], List[List[float]]],
        truncation: bool = True,
        pad_to_multiple_of: Optional[int] = None,
        return_tensors: Optional[Union[str, TensorType]] = None,
        return_attention_mask: Optional[bool] = None,
        padding: Optional[str] = True,
        max_length: Optional[int] = None,
        sampling_rate: Optional[int] = None,
        **kwargs,
    ) -> BatchFeature:
        """
        Main method to featurize and prepare for the model one or several sequence(s).

        Args:
            audio (`mindspore.Tensor`, `np.ndarray`, `List[float]`, `List[np.ndarray]`, `List[mindspore.Tensor]`,
                `List[List[float]]`):
                The sequence or batch of sequences to be padded. Each sequence can be a mindspore tensor, a numpy array,
                a list of float values, a list of numpy arrays, a list of mindspore tensors, or a list of list of float
                values. If `audio` is the output of Demucs, it has to be a mindspore tensor of shape
                `(batch_size, num_stems, channel_size, audio_length)`.
                Otherwise, it must be mono or stereo channel audio.
            truncation (`bool`, *optional*, default to `True`):
                Activates truncation to cut input sequences longer than *max_length* to *max_length*.
            pad_to_multiple_of (`int`, *optional*, defaults to None):
                If set will pad the sequence to a multiple of the provided value.

                This is especially useful to enable the use of Tensor Cores on NVIDIA hardware with compute capability
                `>= 7.5` (Volta), or on TPUs which benefit from having sequence lengths be a multiple of 128.
            return_tensors (`str` or [`~utils.TensorType`], *optional*):
                If set, will return tensors instead of list of python integers. Acceptable values are:

                - `'tf'`: Return TensorFlow `tf.constant` objects.
                - `'pt'`: Return PyTorch `mindspore.Tensor` objects.
                - `'np'`: Return Numpy `np.ndarray` objects.
            return_attention_mask (`bool`, *optional*):
                Whether to return the attention mask. If left to the default, will return the attention mask according
                to the specific feature_extractor's default.

                [What are attention masks?](../glossary#attention-mask)

                <Tip>
                For Musicgen Melody models, audio `attention_mask` is not necessary.
                </Tip>

            padding (`bool`, `str` or [`~utils.PaddingStrategy`], *optional*, defaults to `True`):
                Select a strategy to pad the returned sequences (according to the model's padding side and padding
                index) among:

                - `True` or `'longest'`: Pad to the longest sequence in the batch (or no padding if only a single
                sequence if provided).
                - `'max_length'`: Pad to a maximum length specified with the argument `max_length` or to the maximum
                acceptable input length for the model if that argument is not provided.
                - `False` or `'do_not_pad'` (default): No padding (i.e., can output a batch with sequences of different
                lengths).
            max_length (`int`, *optional*):
                Maximum length of the returned list and optionally padding length (see above).
            sampling_rate (`int`, *optional*):
                The sampling rate at which the `audio` input was sampled. It is strongly recommended to pass
                `sampling_rate` at the forward call to prevent silent errors.
                Note that if `audio` is the output of Demucs, `sampling_rate` must be the sampling rate at which Demucs operates.
        """
        if sampling_rate is None:
            logger.warning_once(
                "It is strongly recommended to pass the `sampling_rate` argument to this function. "
                "Failing to do so can result in silent errors that might be hard to debug."
            )

        if isinstance(audio, mindspore.Tensor) and len(audio.shape) == 4:
            logger.warning_once(
                "`audio` is a 4-dimensional mindspore tensor and has thus been recognized as the output of `Demucs`. "
                "If this is not the case, make sure to read Musicgen Melody docstrings and "
                "to correct `audio` to get the right behaviour."
                "Link to the docstrings: https://huggingface.co/docs/transformers/main/en/model_doc/musicgen_melody"
            )
            audio = self._extract_stem_indices(audio, sampling_rate=sampling_rate)
        elif sampling_rate is not None and sampling_rate != self.sampling_rate:
            transform = Resample(sampling_rate, self.sampling_rate,
                             resample_method=ResampleMethod.SINC_INTERPOLATION,
                             rolloff=0.945, lowpass_filter_width=24)
            audio = transform(audio)

        is_batched = isinstance(audio, (np.ndarray, mindspore.Tensor)) and len(audio.shape) > 1
        is_batched = is_batched or (
            isinstance(audio, (list, tuple)) and (isinstance(audio[0], (mindspore.Tensor, np.ndarray, tuple, list)))
        )

        if is_batched and not isinstance(audio[0], mindspore.Tensor):
            audio = [mindspore.tensor(speech, dtype=mindspore.float32).unsqueeze(-1) for speech in audio]
        elif is_batched:
            audio = [speech.unsqueeze(-1) for speech in audio]
        elif not is_batched and not isinstance(audio, mindspore.Tensor):
            audio = mindspore.tensor(audio, dtype=mindspore.float32).unsqueeze(-1)

        if isinstance(audio[0], mindspore.Tensor) and audio[0].dtype is mindspore.float64:
            audio = [speech.to(mindspore.float32) for speech in audio]

        # always return batch
        if not is_batched:
            audio = [audio]

        if len(audio[0].shape) == 3:
            logger.warning_once(
                "`audio` has been detected as a batch of stereo signals. Will be convert to mono signals. "
                "If this is an undesired behaviour, make sure to read Musicgen Melody docstrings and "
                "to correct `audio` to get the right behaviour."
                "Link to the docstrings: https://huggingface.co/docs/transformers/main/en/model_doc/musicgen_melody"
            )
            # convert to mono-channel waveform
            audio = [stereo.mean(dim=0) for stereo in audio]

        batched_speech = BatchFeature({"input_features": audio})

        padded_inputs = self.pad(
            batched_speech,
            padding=padding,
            max_length=max_length if max_length else self.n_samples,
            truncation=truncation,
            pad_to_multiple_of=pad_to_multiple_of,
            return_attention_mask=return_attention_mask,
            return_tensors="ms",
        )

        input_features = self._ms_extract_fbank_features(padded_inputs["input_features"].squeeze(-1))

        padded_inputs["input_features"] = input_features

        if return_attention_mask:
            # rescale from raw audio length to spectrogram length
            padded_inputs["attention_mask"] = padded_inputs["attention_mask"][:, :: self.hop_length]

        if return_tensors is not None:
            padded_inputs = padded_inputs.convert_to_tensors(return_tensors)

        return padded_inputs

    def to_dict(self) -> Dict[str, Any]:
        """
        Serializes this instance to a Python dictionary. Returns:
            `Dict[str, Any]`: Dictionary of all the attributes that make up this configuration instance.
        """
        output = copy.deepcopy(self.__dict__)
        output["feature_extractor_type"] = self.__class__.__name__
        if "mel_filters" in output:
            del output["mel_filters"]
        if "window" in output:
            del output["window"]
        if "chroma_filters" in output:
            del output["chroma_filters"]
        if "spectrogram" in output:
            del output["spectrogram"]
        return output

__all__ = ['MusicgenMelodyFeatureExtractor']
