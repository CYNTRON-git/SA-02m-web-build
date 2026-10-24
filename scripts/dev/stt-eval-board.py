#!/usr/bin/env python3
"""On-board speech-to-text measurement for SA-02m (docs/decisions/speech-recognition.md).

sherpa-onnx + vosk-model-small-streaming-ru 0.54 (int8 zipformer). Measures model
load time, RSS, MemAvailable, real-time factor, word error rate and command
matching against a fixed command list (difflib, best match >= 0.6).

Board-only dev tool, never deployed and NOT a quality row: it needs the board,
its USB microphone and a ~80 MB sandbox outside the repo. Setup on the board
(as done 2026-10-15 on 1.135, all under /root/stt-eval, nothing system-wide):
  python3 -m venv venv
  ./venv/bin/pip install sherpa-onnx               # cp312 linux_armv7l wheel, no deps
  model files from huggingface.co/alphacep/vosk-model-small-streaming-ru:
    am-onnx/{encoder,decoder,joiner}.int8.onnx, lang/tokens.txt -> <model_dir>
No numpy (PyPI has no armv7l wheel): PCM goes in as a plain list of floats.

Usage: stt-eval-board.py files <model_dir> <wav_dir> <truth.tsv> <threads>
       stt-eval-board.py live  <model_dir> <seconds> <threads>
         (arecord on $SA02M_MIC_DEV, default plughw:1,0 — check `arecord -l`)
truth.tsv: "<file.wav>	<reference text>"; 16 kHz mono 16-bit WAV; a file whose
name contains "-c" is scored as a command.
"""
import difflib
import os
import re
import resource
import subprocess
import sys
import time
import wave

import array
import sherpa_onnx

COMMANDS = [
    "включи свет в гостиной", "выключи свет на кухне", "сделай ярче", "сделай темнее",
    "включи светомузыку", "выключи светомузыку", "запиши сообщение",
    "какая температура в доме", "открой ворота", "режим ночь",
]


def status_kb(key):
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith(key + ":"):
                return int(line.split()[1])
    return -1


def mem_available_mb():
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    return -1


def cpu_s():
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


def make(model_dir, threads):
    return sherpa_onnx.OnlineRecognizer.from_transducer(
        tokens=os.path.join(model_dir, "tokens.txt"),
        encoder=os.path.join(model_dir, "encoder.int8.onnx"),
        decoder=os.path.join(model_dir, "decoder.int8.onnx"),
        joiner=os.path.join(model_dir, "joiner.int8.onnx"),
        num_threads=threads, sample_rate=16000, feature_dim=80,
        decoding_method="greedy_search", enable_endpoint_detection=True,
    )


def norm(t):
    return re.sub(r"[^а-яё ]", "", t.lower().replace("ё", "е")).strip()


def wer(ref, hyp):
    r, h = norm(ref).split(), norm(hyp).split()
    d = [[0] * (len(h) + 1) for _ in range(len(r) + 1)]
    for i in range(len(r) + 1):
        d[i][0] = i
    for j in range(len(h) + 1):
        d[0][j] = j
    for i in range(1, len(r) + 1):
        for j in range(1, len(h) + 1):
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1,
                          d[i - 1][j - 1] + (r[i - 1] != h[j - 1]))
    return d[len(r)][len(h)] / max(1, len(r))


def best_command(text):
    scored = [(difflib.SequenceMatcher(None, norm(text), norm(c)).ratio(), c) for c in COMMANDS]
    return max(scored)


def decode(rec, samples):
    s = rec.create_stream()
    s.accept_waveform(16000, samples)
    s.accept_waveform(16000, [0.0] * int(0.66 * 16000))
    s.input_finished()
    while rec.is_ready(s):
        rec.decode_stream(s)
    return rec.get_result(s).strip()


def pcm16(b):
    a = array.array("h")
    a.frombytes(b)
    return [v / 32768 for v in a]


def read_wav(p):
    with wave.open(p) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2, p
        return pcm16(w.readframes(w.getnframes()))


def cmd_files(model_dir, wav_dir, truth, threads):
    avail0 = mem_available_mb()
    t0 = time.time()
    rec = make(model_dir, threads)
    print(f"load_s={time.time() - t0:.2f} rss_after_load_mb={status_kb('VmRSS') // 1024} avail_before_mb={avail0}")
    rows = [l.rstrip("\n").split("\t") for l in open(truth, encoding="utf-8") if l.strip()]
    tot_audio = tot_wall = tot_cpu = 0.0
    cmd_ok = cmd_n = 0
    for fn, ref in rows:
        x = read_wav(os.path.join(wav_dir, fn))
        dur = len(x) / 16000
        c0, w0 = cpu_s(), time.time()
        hyp = decode(rec, x)
        wall, cpu = time.time() - w0, cpu_s() - c0
        tot_audio += dur; tot_wall += wall; tot_cpu += cpu
        line = f"{fn}\tdur={dur:.2f}s wall={wall:.2f}s rtf={wall / dur:.2f} wer={wer(ref, hyp):.2f}\thyp={hyp!r}"
        if "-c" in fn:
            score, cmd = best_command(hyp)
            ok = cmd == ref and score >= 0.6
            cmd_ok += ok; cmd_n += 1
            line += f"\tcmd={'OK' if ok else 'MISS'}({score:.2f})"
        print(line)
    print(f"SUMMARY threads={threads} audio_s={tot_audio:.1f} wall_s={tot_wall:.1f} rtf={tot_wall / tot_audio:.2f} "
          f"cpu_per_audio_s={tot_cpu / tot_audio:.2f} commands={cmd_ok}/{cmd_n} "
          f"peak_rss_mb={status_kb('VmHWM') // 1024} avail_after_mb={mem_available_mb()}")


def cmd_live(model_dir, seconds, threads):
    rec = make(model_dir, threads)
    s = rec.create_stream()
    p = subprocess.Popen(["arecord", "-q", "-D", os.environ.get("SA02M_MIC_DEV", "plughw:1,0"), "-f", "S16_LE", "-r", "16000", "-c", "1",
                          "-t", "raw", "-d", str(seconds)], stdout=subprocess.PIPE)
    c0, w0, lag_max, n = cpu_s(), time.time(), 0.0, 0
    while True:
        buf = p.stdout.read(3200)  # 100 ms
        if not buf:
            break
        n += len(buf) // 2
        s.accept_waveform(16000, pcm16(buf))
        while rec.is_ready(s):
            rec.decode_stream(s)
        lag_max = max(lag_max, (time.time() - w0) - n / 16000)
        if rec.is_endpoint(s):
            t = rec.get_result(s).strip()
            if t:
                print(f"heard: {t!r}")
            rec.reset(s)
    # Flush the phrase still open when the capture ends (no endpoint seen yet).
    s.accept_waveform(16000, [0.0] * int(0.66 * 16000))
    s.input_finished()
    while rec.is_ready(s):
        rec.decode_stream(s)
    t = rec.get_result(s).strip()
    if t:
        print(f"heard: {t!r}")
    rc = p.wait()
    if rc != 0 or n == 0:
        print(f"ERROR: arecord rc={rc}, samples={n}", file=sys.stderr)
        sys.exit(1)
    wall = time.time() - w0
    print(f"LIVE threads={threads} audio_s={n / 16000:.1f} wall_s={wall:.1f} cpu_s={cpu_s() - c0:.1f} "
          f"cpu_load_pct_of_one_core={100 * (cpu_s() - c0) / wall:.0f} max_lag_s={lag_max:.2f} "
          f"peak_rss_mb={status_kb('VmHWM') // 1024} avail_mb={mem_available_mb()}")


if __name__ == "__main__":
    if sys.argv[1] == "files":
        cmd_files(sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5]))
    else:
        cmd_live(sys.argv[2], int(sys.argv[3]), int(sys.argv[4]))
