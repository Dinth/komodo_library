# Patched copy of app/asr_models/faster_whisper_engine.py from
# onerahmet/openai-whisper-asr-webservice v1.10.0, bind-mounted over the stock
# file by r720-omv/mediarr/compose.yaml.
#
# Bazarr's whisperai provider only ever sends task/language/output/encode, and
# the webservice has no env to change transcription defaults. Stock settings
# (no VAD, segment-level timestamps, conditioning on previous text) invented
# dialogue over music-only stretches and drifted cue timings by seconds.
#
# Two changes:
# 1. Forced options in transcribe():
#      vad_filter=True                      Silero VAD (bundled) drops non-speech
#      word_timestamps=True                 cue start/end snapped to spoken words
#      hallucination_silence_threshold=2.0  skip text invented over >2s of silence
#    condition_on_previous_text is deliberately left at its default (True).
#    Forcing it False (2026-09-13, 009-1 S01) stopped Whisper carrying
#    punctuation/case between windows: 30-44% of cues came out as lowercase,
#    unpunctuated 10s run-ons mixing speakers. VAD + the silence threshold are
#    what curb looping hallucinations here.
# 2. WhisperModel.find_alignment is replaced with faster-whisper 1.2.1's own
#    implementation plus the empty-alignment guard from upstream PR #1460
#    (https://github.com/SYSTRAN/faster-whisper/pull/1460). Without it,
#    word_timestamps 500s on some episodes with "IndexError: boolean index did
#    not match indexed array" at time_indices[jumps]. Remove once the image
#    ships a faster-whisper release containing #1460.
# 3. Long segments are split into word-timed sub-cues before writing. WriteSRT
#    in app/utils.py emits one cue per segment and ignores word timings, so a
#    passage Whisper leaves unpunctuated became a 10s wall of text. Cues now
#    break after sentence punctuation, on a >=1s pause between words, or before
#    84 chars / 7s. Segments without word timings are left as they are.
# 4. A punctuated initial_prompt is used when the request has none. With
#    condition_on_previous_text on, Whisper keeps whatever style the first
#    window has: 009-1 S01E06 came out 20% unpunctuated on one run and 100%
#    lowercase with no punctuation on the next, same settings. Priming the
#    first window with normal sentence punctuation is the standard remedy.
#
# This file is tied to v1.10.0 (faster-whisper 1.2.1). On an image bump,
# re-diff both the engine file and find_alignment before deploying.

import time
from io import StringIO
from threading import Thread
from typing import BinaryIO, Union

import whisper
from faster_whisper import WhisperModel

from app.asr_models.asr_model import ASRModel
from app.config import CONFIG
from app.utils import ResultWriter, WriteJSON, WriteSRT, WriteTSV, WriteTXT, WriteVTT

import numpy as np
import faster_whisper.transcribe as _fw_transcribe


# faster-whisper 1.2.1 find_alignment + PR #1460 guard (see header).
def _find_alignment_guarded(
    self,
    tokenizer,
    text_tokens,
    encoder_output,
    num_frames,
    median_filter_width=7,
):
    if len(text_tokens) == 0:
        return []

    results = self.model.align(
        encoder_output,
        tokenizer.sot_sequence,
        text_tokens,
        num_frames,
        median_filter_width=median_filter_width,
    )
    return_list = []
    for result, text_token in zip(results, text_tokens):
        text_token_probs = result.text_token_probs
        alignments = result.alignments
        if len(alignments) == 0:
            # PATCH (upstream PR #1460): align() can return no alignment for a
            # tiny window; give that segment no word timestamps instead of
            # crashing on time_indices[jumps].
            return_list.append([])
            continue
        text_indices = np.array([pair[0] for pair in alignments])
        time_indices = np.array([pair[1] for pair in alignments])

        words, word_tokens = tokenizer.split_to_word_tokens(
            text_token + [tokenizer.eot]
        )
        if len(word_tokens) <= 1:
            # return on eot only
            # >>> np.pad([], (1, 0))
            # array([0.])
            # This results in crashes when we lookup jump_times with float, like
            # IndexError: arrays used as indices must be of integer (or boolean) type
            return_list.append([])
            continue
        word_boundaries = np.pad(
            np.cumsum([len(t) for t in word_tokens[:-1]]), (1, 0)
        )
        if len(word_boundaries) <= 1:
            return_list.append([])
            continue

        jumps = np.pad(np.diff(text_indices), (1, 0), constant_values=1).astype(
            bool
        )
        jump_times = time_indices[jumps] / self.tokens_per_second
        start_times = jump_times[word_boundaries[:-1]]
        end_times = jump_times[word_boundaries[1:]]
        word_probabilities = [
            np.mean(text_token_probs[i:j])
            for i, j in zip(word_boundaries[:-1], word_boundaries[1:])
        ]

        return_list.append(
            [
                dict(
                    word=word,
                    tokens=tokens,
                    start=start,
                    end=end,
                    probability=probability,
                )
                for word, tokens, start, end, probability in zip(
                    words, word_tokens, start_times, end_times, word_probabilities
                )
            ]
        )
    return return_list


_fw_transcribe.WhisperModel.find_alignment = _find_alignment_guarded


from dataclasses import replace

# Primes Whisper's first window with normal sentence punctuation (header item 4).
DEFAULT_INITIAL_PROMPT = "Hello. Welcome back, everyone! Let's begin."

# Readable-subtitle limits: two 42-char lines, ~7s on screen, break on a real pause.
CUE_MAX_CHARS = 84
CUE_MAX_SECONDS = 7.0
CUE_PAUSE_SECONDS = 1.0
CUE_MIN_CHARS_AT_SENTENCE_END = 15


def _split_long_segments(segments):
    """Split segments that are too long to read into word-timed sub-cues (see header)."""
    out = []
    for seg in segments:
        words = [w for w in (seg.words or []) if w.word.strip()]
        text = seg.text.strip()
        if not words or (len(text) <= CUE_MAX_CHARS and seg.end - seg.start <= CUE_MAX_SECONDS):
            out.append(seg)
            continue
        chunks, cur = [], []
        for w in words:
            if cur:
                cur_text = "".join(x.word for x in cur).strip()
                too_long = len(cur_text + w.word) > CUE_MAX_CHARS
                too_slow = w.end - cur[0].start > CUE_MAX_SECONDS
                paused = w.start - cur[-1].end >= CUE_PAUSE_SECONDS
                sentence_end = cur_text[-1:] in ".!?…" and len(cur_text) >= CUE_MIN_CHARS_AT_SENTENCE_END
                if too_long or too_slow or paused or sentence_end:
                    chunks.append(cur)
                    cur = []
            cur.append(w)
        if cur:
            chunks.append(cur)
        for chunk in chunks:
            out.append(replace(
                seg,
                start=chunk[0].start,
                end=max(chunk[-1].end, chunk[0].start + 0.3),
                text="".join(x.word for x in chunk).strip(),
                tokens=[],
                words=chunk,
            ))
    for i, seg in enumerate(out, start=1):
        seg.id = i
    return out


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
        # Bazarr never sends a prompt; default to punctuated text (see header).
        options_dict["initial_prompt"] = initial_prompt or DEFAULT_INITIAL_PROMPT
        # Forced regardless of the request: Bazarr cannot pass these (see header).
        options_dict["vad_filter"] = True
        options_dict["word_timestamps"] = True
        options_dict["hallucination_silence_threshold"] = 2.0
        with self.model_lock:
            segments = []
            text = ""
            segment_generator, info = self.model.transcribe(audio, beam_size=5, **options_dict)
            for segment in segment_generator:
                segments.append(segment)
                text = text + segment.text
            result = {
                "language": options_dict.get("language", info.language),
                "segments": _split_long_segments(segments),
                "text": text,
            }

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
