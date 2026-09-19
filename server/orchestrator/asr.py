"""ASR client — posts a WAV to the vLLM OpenAI-compatible transcription
endpoint at Qwen3-ASR's native 24 kHz."""

import io
import logging

import httpx
import soundfile as sf

log = logging.getLogger(__name__)

# Qwen3-ASR returns "language English<asr_text>transcribed words". The
# metadata prefix has to go before the text reaches the LLM.
_ASR_TEXT_TAG = "<asr_text>"
_LANG_PREFIX = "language "

# Languages the recogniser may answer in, ISO 639-1 in the order they are
# tried. Qwen3-ASR identifies the spoken language itself, and on a
# one-syllable word it often names a third one: a child's "my" came back as
# Thai and the tutor answered in Thai. vLLM's endpoint can only bias the
# decode, not restrict it, so a reading outside these two is taken again
# with each of them forced in turn, and dropped if none of the readings is
# in a script anyone here is speaking.
DEFAULT_LANGUAGES = ("en", "zh")

# The endpoint takes an ISO 639-1 code in to_language; Qwen3-ASR writes the
# full name into the prompt it decodes from.
_LANGUAGE_NAMES = {"en": "English", "zh": "Chinese"}

# A forced decode pre-fills the "language X<asr_text>" prefix, so the second
# reading carries no language to parse and is checked by script instead.
_CJK = ((0x3000, 0x303F), (0x3400, 0x4DBF), (0x4E00, 0x9FFF),
        (0xF900, 0xFAFF), (0xFF01, 0xFF60))
_LATIN_MAX = 0x024F          # Latin and its extensions


def parse_transcript(raw: str) -> tuple[str | None, str]:
    """Split a Qwen3-ASR response into (language, text).

    The language is None when the decode was forced: the tag that carries it
    is then part of the prompt rather than of the answer.
    """
    lang = None
    if _ASR_TEXT_TAG in raw:
        head, raw = raw.split(_ASR_TEXT_TAG, 1)
        head = head.strip()
        if head.startswith(_LANG_PREFIX):
            lang = head[len(_LANG_PREFIX):].strip()
    return lang, raw.strip()


def in_scope(text: str) -> bool:
    """True when every letter in the text is Latin or CJK."""
    for ch in text:
        if not ch.isalpha():
            continue
        if ord(ch) <= _LATIN_MAX:
            continue
        if any(lo <= ord(ch) <= hi for lo, hi in _CJK):
            continue
        return False
    return True


class ASRClient:
    def __init__(self, base_url: str, model: str,
                 languages=DEFAULT_LANGUAGES):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.languages = tuple(languages)
        if unknown := [c for c in self.languages if c not in _LANGUAGE_NAMES]:
            raise ValueError(f"unsupported ASR languages: {unknown}")
        self.accepted = frozenset(_LANGUAGE_NAMES[c] for c in self.languages)
        self._client = httpx.AsyncClient(timeout=60.0, trust_env=False)

    async def transcribe(self, pcm16, sr: int = 16000) -> str:
        wav = self._encode(pcm16, sr)
        lang, text = parse_transcript(await self._post(wav, None))
        if lang in self.accepted and in_scope(text):
            return text
        log.info("asr read %r, re-reading forced", lang)
        for code in self.languages:
            _, text = parse_transcript(await self._post(wav, code))
            if in_scope(text):
                return text
        # Nothing here is worth saying in a third language: the tutor takes
        # the transcript at face value and teaches away from the plan.
        return ""

    @staticmethod
    def _encode(pcm16, sr: int) -> bytes:
        buf = io.BytesIO()
        sf.write(buf, pcm16, sr, format="WAV", subtype="PCM_16")
        return buf.getvalue()

    async def _post(self, wav: bytes, to_language: str | None) -> str:
        data = {"model": self.model}
        if to_language:
            data["to_language"] = to_language
        resp = await self._client.post(
            f"{self.base_url}/audio/transcriptions",
            files={"file": ("utterance.wav", wav, "audio/wav")},
            data=data,
        )
        resp.raise_for_status()
        return resp.json().get("text", "")
