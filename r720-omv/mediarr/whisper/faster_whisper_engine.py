# Patched copy of app/asr_models/faster_whisper_engine.py from
# onerahmet/openai-whisper-asr-webservice v1.10.0, bind-mounted over the stock
# file by r720-omv/mediarr/compose.yaml.
#
# Bazarr's whisperai provider only ever sends task/language/output/encode, and
# the webservice has no env to change transcription defaults. Stock settings
# (no VAD, segment-level timestamps, conditioning on previous text) invented
# dialogue over music-only stretches and drifted cue timings by seconds. The
# only change is the block of forced options in transcribe():
#   vad_filter=True                      Silero VAD (bundled) drops non-speech
#   word_timestamps=True                 cue start/end snapped to spoken words
#   hallucination_silence_threshold=2.0  skip text invented over >2s of silence
#   condition_on_previous_text=False     one hallucination can't seed the next
#
# This file is tied to v1.10.0. On an image bump, re-diff it against the new
# upstream file before deploying.

import time
from io import StringIO
from threading import Thread
from typing import BinaryIO, Union

import whisper
from faster_whisper import WhisperModel

from app.asr_models.asr_model import ASRModel
from app.config import CONFIG
from app.utils import ResultWriter, WriteJSON, WriteSRT, WriteTSV, WriteTXT, WriteVTT


class FasterWhisperASR(ASRModel):

    def load_model(self):

        self.model = WhisperModel(
            model_size_or_path=CONFIG.MODEL_NAME,
            device=CONFIG.DEVICE,
            compute_type=CONFIG.MODEL_QUANTIZATION,
            download_root=CONFIG.MODEL_PATH
        )

        Thread(target=self.monitor_idleness, daemon=True).start()

    def transcribe(
            self,
            audio,
            task: Union[str, None],
            language: Union[str, None],
            initial_prompt: Union[str, None],
            vad_filter: Union[bool, None],
            word_timestamps: Union[bool, None],
            options: Union[dict, None],
            output,
    ):
        self.last_activity_time = time.time()

        with self.model_lock:
            if self.model is None:
                self.load_model()

        options_dict = {"task": task}
        if language:
            options_dict["language"] = language
        if initial_prompt:
            options_dict["initial_prompt"] = initial_prompt
        # Forced regardless of the request: Bazarr cannot pass these (see header).
        options_dict["vad_filter"] = True
        options_dict["word_timestamps"] = True
        options_dict["hallucination_silence_threshold"] = 2.0
        options_dict["condition_on_previous_text"] = False
        with self.model_lock:
            segments = []
            text = ""
            segment_generator, info = self.model.transcribe(audio, beam_size=5, **options_dict)
            for segment in segment_generator:
                segments.append(segment)
                text = text + segment.text
            result = {"language": options_dict.get("language", info.language), "segments": segments, "text": text}

        output_file = StringIO()
        self.write_result(result, output_file, output)
        output_file.seek(0)

        return output_file

    def language_detection(self, audio):

        self.last_activity_time = time.time()

        with self.model_lock:
            if self.model is None: self.load_model()

        # load audio and pad/trim it to fit 30 seconds
        audio = whisper.pad_or_trim(audio)

        # detect the spoken language
        with self.model_lock:
            segments, info = self.model.transcribe(audio, beam_size=5)
            detected_lang_code = info.language
            detected_language_confidence = info.language_probability

        return detected_lang_code, detected_language_confidence

    def write_result(self, result: dict, file: BinaryIO, output: Union[str, None]):
        if output == "srt":
            WriteSRT(ResultWriter).write_result(result, file=file)
        elif output == "vtt":
            WriteVTT(ResultWriter).write_result(result, file=file)
        elif output == "tsv":
            WriteTSV(ResultWriter).write_result(result, file=file)
        elif output == "json":
            WriteJSON(ResultWriter).write_result(result, file=file)
        else:
            WriteTXT(ResultWriter).write_result(result, file=file)
