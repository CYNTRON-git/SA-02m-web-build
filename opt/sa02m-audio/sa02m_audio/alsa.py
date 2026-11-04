"""Which ALSA device to record from (contract §Устройство захвата).

The card index is not stable across a replug or a reboot
(docs/usb-audio-microphone.md), so `device=auto` is resolved at EACH start
from `/proc/asound/cards`: the first `USB-Audio` card that has a capture PCM,
as `plughw:CARD=<id>,DEV=0` (bench 1.135: `plughw:CARD=Microphone,DEV=0`
records), falling back to `plughw:<index>,0` when the id is not a plain ALSA
id. `plughw` is required: the 16 kHz the recorder asks for comes from the plug
layer's resampling (the Fifine records 44.1–192 kHz natively).
"""

from __future__ import annotations

import os
import re
from collections import namedtuple
from typing import List, Optional, Tuple

from . import constants as C

Card = namedtuple("Card", "index id driver name")

# ` 1 [Microphone     ]: USB-Audio - Fifine Microphone`
_CARD_LINE = re.compile(r"^\s*(\d+)\s+\[([^\]]*)\]:\s*(\S+)\s+-\s+(.*?)\s*$")
_CARD_ID = re.compile(C.CARD_ID_RE)
_CAPTURE_PCM = re.compile(r"^pcm\d+c$")


def parse_cards(text: str) -> List[Card]:
    cards = []
    for line in text.splitlines():
        m = _CARD_LINE.match(line)
        if m:
            cards.append(Card(int(m.group(1)), m.group(2).strip(), m.group(3), m.group(4)))
    return cards


def has_capture(index: int, asound_dir: str = C.ASOUND_DIR) -> bool:
    try:
        names = os.listdir(os.path.join(asound_dir, "card%d" % index))
    except OSError:
        return False
    return any(_CAPTURE_PCM.match(n) for n in names)


def device_name(card: Card) -> str:
    if _CARD_ID.match(card.id):
        return "plughw:CARD=%s,DEV=0" % card.id
    return "plughw:%d,0" % card.index


def read_cards(asound_dir: str = C.ASOUND_DIR) -> List[Card]:
    try:
        with open(os.path.join(asound_dir, "cards"), encoding="utf-8", errors="replace") as fh:
            return parse_cards(fh.read(65536))
    except OSError:
        return []


def resolve(conf_device: str, asound_dir: str = C.ASOUND_DIR) -> Tuple[Optional[str], Optional[Card]]:
    """(ALSA device string, the card) or (None, None) when no mic is found.

    An explicit conf device (already allow-listed by config) is used as is;
    the mic then counts as present when ANY card has a capture PCM — which
    card the name points at is ALSA's to say, at start.
    """
    cards = [c for c in read_cards(asound_dir) if has_capture(c.index, asound_dir)]
    if conf_device != C.DEVICE_AUTO:
        return (conf_device, cards[0]) if cards else (None, None)
    for card in cards:
        if card.driver == C.USB_AUDIO_DRIVER:
            return device_name(card), card
    return None, None
