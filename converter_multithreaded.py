#!/usr/bin/env python3
# Multithreaded Library Manager
# This script will convert album tracks recursively (depending on choices) and output files inside each folder respectively, creating a folder for outputs per file format.
# I tried to make this as easy to understand for anyone looking at it or future me, but I am braindead.

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Set
import fnmatch

# ----------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent.resolve()
CONFIG_FILE = SCRIPT_DIR / "flac_batch_conv.conf"
FILTER_PRESET_FILE = SCRIPT_DIR / "folder_filter_preset.conf"

# this is the default config. it makes a default config. The settings should match Opus at its defaults in terms of quality, and is the standard I wish to assume.
DEFAULT_CONFIG = {
    "last_source": "",
    "formats": ["opus"],
    # multithread settings
    "cpu_usage_percent": 75,
    "multithread": True,
    # Opus (VBR, CVBR, CBR)
    "opus_mode": "vbr",
    "opus_vbr_bitrate": "128k",
    "opus_cvbr_bitrate": "128k",
    "opus_cbr_bitrate": "128k",
    "opus_application": "audio",
    "opus_extra": "",
    # MP3 (VBR, ABR, CBR)
    "mp3_mode": "vbr",
    "mp3_vbr_quality": "2",
    "mp3_abr_bitrate": "192k",
    "mp3_cbr_bitrate": "192k",
    "mp3_extra": "",
    # AAC (VBR, ABR, CBR)
    "aac_mode": "vbr",
    "aac_vbr_quality": "2",
    "aac_abr_bitrate": "160k",
    "aac_cbr_bitrate": "160k",
    "aac_extra": "",
    # Ogg (VBR, ABR, CBR)
    "ogg_mode": "vbr",
    "ogg_vbr_quality": "6",
    "ogg_abr_bitrate": "192k",
    "ogg_cbr_bitrate": "192k",
    "ogg_extra": "",
    # WAV
    "wav_bit_depth": "keep",    # "keep", "16", "24", "32"
    "wav_extra": "",
    # General
    "overwrite": False,
    "ffmpeg_verbose": False,
    "mutagen_verbose": False,
    "embed_cover_art": True,
    "prioritize_embedded_cover": True,
    "sample_rate": "keep",
    "channels": "keep",           # legacy: kept for backward compatibility, but now channel_handling overrides
    "channel_handling": "auto",   # "auto", "stereo", "keep"
}

# print lock. this prevents multiple threads from printing to terminal at the same time
print_lock = threading.Lock()


def display_bitrate(bitrate: str) -> str:
    # make 128k into 128kbps because idk its more accurate ig? i just wanted it to
    if bitrate.endswith('k'):
        return bitrate[:-1] + "kbps"
    return bitrate


# making sure threads dont fuck up printing
def safe_print(*args, **kwargs):
    with print_lock:
        print(*args, **kwargs)


# this function normalises sample rates (as the name suggests) by multiplication. if you input 44.1k, its multiplied by 1000 and you get 44100.
# its just to make entering sample rates more intuitive ig
def normalize_sample_rate(sr: str) -> str:
    sr = sr.strip().lower()
    if sr == "keep":
        return sr
    match = re.match(r'^(\d+(?:\.\d+)?)\s*k$', sr)
    if match:
        value = float(match.group(1))
        return str(int(value * 1000))
    try:
        return str(int(float(sr)))
    except ValueError:
        return "keep"
"""
    r'...' = raw string
    ^ = start of string
    (\d+(?:.\d+)?) = checking for number part (like uhh 44.1 or 48
    \d+ = checking for one or more digits
    (?:.\d+)? = checking for decimal and extra numbers after decimal
    \s* = checking for whitespace
    k = checking for "k" (case-insensitive)
    $ = end of string
"""


# You won't believe what this function does!™
def normalize_bitrate(bitrate: str) -> Optional[str]:
    # "128k" for "128", "128k", "128kbps"
    # None for invalid input
    bitrate = bitrate.strip().lower()
    if not bitrate:
        return None

    # remove 'bps' if present
    if bitrate.endswith('bps'):
        bitrate = bitrate[:-3]

    # number followed by k
    match = re.match(r'^(\d+(?:\.\d+)?)\s*k?$', bitrate)
    if match:
        number = match.group(1)
        try:
            # If whole number, use integer
            if '.' in number:
                # Convert to float then to integer (round)
                value = float(number)
                int_value = int(round(value))
                return f"{int_value}k"
            else:
                return f"{number}k"
        except:
            return None
    return None


# this one loads the config. the config file is stored in the cwd of the program.
def load_config() -> Dict:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r") as f:
                data = json.load(f)
                bitrate_keys = [
                    "opus_vbr_bitrate", "opus_cvbr_bitrate", "opus_cbr_bitrate",
                    "mp3_abr_bitrate", "mp3_cbr_bitrate",
                    "aac_abr_bitrate", "aac_cbr_bitrate",
                    "ogg_abr_bitrate", "ogg_cbr_bitrate",
                ]
                for key in bitrate_keys:
                    if key in data:
                        data[key] = normalize_bitrate(data[key])
                if "sample_rate" in data:
                    data["sample_rate"] = normalize_sample_rate(data["sample_rate"])
                if "cpu_usage_percent" not in data:
                    data["cpu_usage_percent"] = DEFAULT_CONFIG["cpu_usage_percent"]
                if "multithread" not in data:
                    data["multithread"] = DEFAULT_CONFIG["multithread"]
                if "channels" not in data:
                    data["channels"] = DEFAULT_CONFIG["channels"]
                if "wav_bit_depth" not in data:
                    data["wav_bit_depth"] = DEFAULT_CONFIG["wav_bit_depth"]
                if "channel_handling" not in data:
                    data["channel_handling"] = DEFAULT_CONFIG["channel_handling"]
                return {**DEFAULT_CONFIG, **data}
        except:
            return DEFAULT_CONFIG.copy()
    return DEFAULT_CONFIG.copy()


def save_config(config: Dict):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


# clears the screen.
def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')


def print_header(title="FLAC Batch Converter"):
    clear_screen()
    print("=" * 60)
    print(title.center(60))
    print("=" * 60)


# checks for mutagen, unfortunately fucking ffmpeg does not support adding cover art to opus and ogg files
#  I get it uses a different method, but does it hurt to make it a thing?
# ALSO LITTLE FUCKING RANT HERE, WHY DOES MICROSLOP NOT SUPPORT OPUS AND OGG BY FUCKING DEFAULT? IT CANT READ THE FUCKING METADATA WHAT A SHITT FUCKING OPERATING SYSTEM.
def check_mutagen():
    try:
        import mutagen
        return True
    except ImportError:
        safe_print("\nWARNING: 'mutagen' not installed. Some formats may not correctly save metadata!")
        safe_print("Install with: pip install mutagen")
        time.sleep(2)
        return False


# calculates max amount of workers from total threads then sets 75% of those threads
# (or ig cpu count i think os.cpu_count replies with threads instead of actual cores)
# i thought 75% is a good percentage.
def compute_max_workers(config: Dict) -> int:
    percent = config.get("cpu_usage_percent", 75)
    cores = os.cpu_count() or 4
    workers = max(1, int(cores * percent / 100))
    return workers


# -- cover art functions {
def extract_cover_art(src: Path, temp_dir: Path, track_name: str) -> Optional[Path]:
    cover_file = temp_dir / f"cover_{track_name}.jpg"
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-y", "-i", str(src), "-map", "0:v?", "-c:v", "copy",
        "-frames:v", "1", str(cover_file)
    ]
    try:
        subprocess.run(cmd, check=True, stdin=subprocess.DEVNULL, capture_output=True)
        if cover_file.exists() and cover_file.stat().st_size > 0:
            return cover_file
    except:
        pass
    return None


def get_cover_art(src: Path, temp_dir: Path, track_name: str,
                  prioritize_embedded: bool, verbose: bool) -> Optional[Path]:
    album_dir = src.parent
    external_candidates = [album_dir / "cover.jpg", album_dir / "folder.jpg", album_dir / "Cover.jpg"]
    embedded = extract_cover_art(src, temp_dir, track_name)

    if prioritize_embedded:
        if embedded:
            if verbose:
                safe_print("    Pulled cover from FLAC")
            return embedded
        for candidate in external_candidates:
            if candidate.exists() and candidate.stat().st_size > 0:
                temp_cover = temp_dir / f"cover_{track_name}.jpg"
                shutil.copy(candidate, temp_cover)
                if verbose:
                    safe_print(f"   Found external cover: {candidate.name}")
                return temp_cover
    else:
        for candidate in external_candidates:
            if candidate.exists() and candidate.stat().st_size > 0:
                temp_cover = temp_dir / f"cover_{track_name}.jpg"
                shutil.copy(candidate, temp_cover)
                if verbose:
                    safe_print(f"   Found external cover: {candidate.name}")
                return temp_cover
        if embedded:
            if verbose:
                safe_print("    Pulled cover from FLAC")
            return embedded
    return None


# fucking shitty ass solution that i have to do cus once again ffmpeg does not support cover arts for opus and ogg!!!!!!!!!!!!!!
def add_cover_art_mutagen(audio_file: Path, cover_file: Path, fmt: str, verbose: bool) -> bool:
    if fmt == "wav":
        return True  # wav dont have cover art and shit
    import time
    from mutagen import File
    from mutagen.flac import Picture
    from mutagen.id3 import ID3, APIC
    from mutagen.mp4 import MP4, MP4Cover
    from mutagen.oggopus import OggOpus
    from mutagen.oggvorbis import OggVorbis
    import base64

    time.sleep(0.2)
    if not audio_file.exists():
        return False

    with open(cover_file, "rb") as f:
        cover_data = f.read()
    mime = "image/jpeg" if cover_file.suffix.lower() in (".jpg", ".jpeg") else "image/png"

    if fmt == "mp3":
        try:
            audio = ID3(audio_file)
        except Exception:
            audio = ID3()
            audio.save(audio_file)
            audio = ID3(audio_file)
        audio.add(APIC(encoding=3, mime=mime, type=3, desc="Front cover", data=cover_data))
        audio.save(v2_version=3)
        if verbose:
            safe_print("    (mutagen) Added cover to MP3")
        return True
    elif fmt == "aac":
        audio = MP4(audio_file)
        cover_type = 0 if mime == "image/jpeg" else 1
        audio["covr"] = [MP4Cover(cover_data, imageformat=cover_type)]
        audio.save()
        if verbose:
            safe_print("    (mutagen) Added cover to AAC")
        return True
    elif fmt in ("opus", "ogg"):
        if fmt == "opus":
            audio = OggOpus(audio_file)
        else:
            audio = OggVorbis(audio_file)
        # Do NOT clear existing tags – only add the picture
        pic = Picture()
        pic.data = cover_data
        pic.mime = mime
        pic.type = 3
        encoded = base64.b64encode(pic.write()).decode("ascii")
        audio["metadata_block_picture"] = encoded
        audio.save()
        if verbose:
            safe_print(f"   (mutagen) Added cover to {fmt.upper()}")
        return True
    return False
# -- cover art functions }


# -- Helper func (alot of these are), get flac bit depth via ffprobe
def get_flac_bit_depth(flac_file: Path) -> str:
    # Return '16', '24', '32', etc. Default '16' on error.
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=bits_per_raw_sample", "-of", "default=noprint_wrappers=1:nokey=1",
        str(flac_file)
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        bits = result.stdout.strip()
        if bits and bits.isdigit():
            return bits
    except:
        pass
    # if failed: try bits_per_sample
    cmd2 = [
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=bits_per_sample", "-of", "default=noprint_wrappers=1:nokey=1",
        str(flac_file)
    ]
    try:
        result = subprocess.run(cmd2, capture_output=True, text=True, check=True)
        bits = result.stdout.strip()
        if bits and bits.isdigit():
            return bits
    except:
        pass
    return "16"  # 16 is a good number and therefore a great default


# get channel count from FLAC
def get_flac_channel_count(flac_file: Path) -> int:
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=channels", "-of", "default=noprint_wrappers=1:nokey=1",
        str(flac_file)
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        channels = result.stdout.strip()
        if channels and channels.isdigit():
            return int(channels)
    except:
        pass
    return 2  # default to stereo


# ffmpeg argument shit. it decodes all the config into ffmpeg arguments. (returns complete args including -c:a)
# just, so many if statements.
# I think the whole func here is pretty easy to understand, its just checking config and converting it to arguments, so i didnt add many comments.
def get_ffmpeg_args(fmt: str, config: Dict, sample_rate: str, src: Optional[Path] = None) -> Tuple[List[str], str]:
    args = []
    if sample_rate != "keep":
        args = ["-ar", sample_rate]

    # channel type (mode? type? idk i like the word type more)
    # legacy channels key kept for compatibility, but we use channel_handling
    channel_handling = config.get("channel_handling", "auto")
    if fmt == "opus" and channel_handling == "auto" and src is not None:
        channels = get_flac_channel_count(src)
        if channels > 2:
            args += ["-ac", str(channels), "-mapping_family", "1"]
        # else do nothing – let ffmpeg default to stereo
    elif channel_handling == "stereo":
        args += ["-ac", "2"]
    elif config.get("channels") == "mono":
        args += ["-ac", "1"]
    elif config.get("channels") == "stereo":
        args += ["-ac", "2"]
    # else "keep" – no -ac flag

    if fmt == "opus":
        args += ["-c:a", "libopus"]
        mode = config["opus_mode"]
        if mode == "vbr":
            bitrate = normalize_bitrate(config["opus_vbr_bitrate"])
            args += ["-b:a", bitrate, "-vbr", "on"]
            desc = f"VBR {display_bitrate(bitrate)}"
        elif mode == "cvbr":
            bitrate = normalize_bitrate(config["opus_cvbr_bitrate"])
            args += ["-b:a", bitrate, "-vbr", "constrained"]
            desc = f"CVBR {display_bitrate(bitrate)}"
        else:
            bitrate = normalize_bitrate(config["opus_cbr_bitrate"])
            args += ["-b:a", bitrate, "-vbr", "off"]
            desc = f"CBR {display_bitrate(bitrate)}"
        if config["opus_application"] in ("audio", "voip", "lowdelay"):
            args += ["-application", config["opus_application"]]
            desc += f", app={config['opus_application']}"
        if config["opus_extra"].strip():
            extra = config["opus_extra"].split()
            args.extend(extra)
            desc += f" + {config['opus_extra']}"
        return args, desc

    elif fmt == "mp3":
        args += ["-c:a", "libmp3lame"]
        mode = config["mp3_mode"]
        if mode == "vbr":
            args += ["-q:a", config["mp3_vbr_quality"]]
            desc = f"VBR quality {config['mp3_vbr_quality']}"
        elif mode == "abr":
            bitrate = normalize_bitrate(config["mp3_abr_bitrate"])
            args += ["-b:a", bitrate, "-abr", "1"]
            desc = f"ABR {display_bitrate(bitrate)}"
        else:
            bitrate = normalize_bitrate(config["mp3_cbr_bitrate"])
            args += ["-b:a", bitrate]
            desc = f"CBR {display_bitrate(bitrate)}"
        if config["mp3_extra"].strip():
            extra = config["mp3_extra"].split()
            args.extend(extra)
            desc += f" + {config['mp3_extra']}"
        return args, desc

    elif fmt == "aac":
        args += ["-c:a", "aac"]
        mode = config["aac_mode"]
        if mode == "vbr":
            args += ["-q:a", config["aac_vbr_quality"]]
            desc = f"VBR quality {config['aac_vbr_quality']}"
        elif mode == "abr":
            bitrate = normalize_bitrate(config["aac_abr_bitrate"])
            args += ["-b:a", bitrate, "-abr", "1"]
            desc = f"ABR {display_bitrate(bitrate)}"
        else:
            bitrate = normalize_bitrate(config["aac_cbr_bitrate"])
            args += ["-b:a", bitrate]
            desc = f"CBR {display_bitrate(bitrate)}"
        if config["aac_extra"].strip():
            extra = config["aac_extra"].split()
            args.extend(extra)
            desc += f" + {config['aac_extra']}"
        return args, desc

    elif fmt == "ogg":
        args += ["-c:a", "libvorbis"]
        mode = config["ogg_mode"]
        if mode == "vbr":
            args += ["-q:a", config["ogg_vbr_quality"]]
            desc = f"VBR quality {config['ogg_vbr_quality']}"
        elif mode == "abr":
            bitrate = normalize_bitrate(config["ogg_abr_bitrate"])
            args += ["-b:a", bitrate, "-abr", "1"]
            desc = f"ABR {display_bitrate(bitrate)}"
        else:
            bitrate = normalize_bitrate(config["ogg_cbr_bitrate"])
            args += ["-b:a", bitrate]
            desc = f"CBR {display_bitrate(bitrate)}"
        if config["ogg_extra"].strip():
            extra = config["ogg_extra"].split()
            args.extend(extra)
            desc += f" + {config['ogg_extra']}"
        return args, desc

    elif fmt == "wav":
        depth_setting = config.get("wav_bit_depth", "keep")
        if depth_setting == "keep" and src is not None:
            depth = get_flac_bit_depth(src)
        elif depth_setting.isdigit():
            depth = depth_setting
        else:
            depth = "16" # fallback, 16 is a very beatiful number
        # Map bit depth to da codec
        if depth == "24":
            args += ["-c:a", "pcm_s24le"]
            desc = "24-bit WAV"
        elif depth == "32":
            args += ["-c:a", "pcm_s32le"]
            desc = "32-bit WAV"
        else:
            args += ["-c:a", "pcm_s16le"]
            desc = "16-bit WAV"
        if config["wav_extra"].strip():
            extra = config["wav_extra"].split()
            args.extend(extra)
            desc += f" + {config['wav_extra']}"
        return args, desc

    raise ValueError(f"Unknown format: {fmt}")


def convert_audio(src: Path, dst: Path, ff_args: List[str],
                  overwrite: bool, verbose: bool) -> bool:
    dst.parent.mkdir(parents=True, exist_ok=True)

    # If overwrite is False, check if destination exists AND has non-zero size
    if not overwrite and dst.exists():
        if dst.stat().st_size > 0:
            if verbose:
                safe_print(f"  SKIP {dst.name}")
            return False
        # If size is zero, treat as missing and continue

    cmd = [
        "ffmpeg", "-hide_banner",
        "-loglevel", "error" if not verbose else "info",
        "-y" if overwrite else "-n",
        "-i", str(src),
        "-map_metadata", "0",
        "-map", "0:a",
    ] + ff_args + [str(dst)]

    if verbose:
        safe_print(f"  Running: {' '.join(cmd)}")
    try:
        subprocess.run(cmd, check=True, stdin=subprocess.DEVNULL,
                       capture_output=not verbose)
        return True
    except subprocess.CalledProcessError as e:
        safe_print(f"  FAIL audio conversion: {src.name} ({e})")
        return False


# Worker func boogaloo
# i honestly forgot what any of this means - comment later? idk
def process_one_file(index: int, src: Path, dst: Path, fmt: str,
                     ff_args: List[str], cover_priority: bool,
                     embed_cover: bool, mutagen_verbose: bool,
                     overwrite: bool, ffmpeg_verbose: bool) -> Tuple[int, Path, Path, bool, str]:
    success = convert_audio(src, dst, ff_args, overwrite, ffmpeg_verbose)
    if not success:
        return index, src, dst, False, f"   Audio Conversion FAIL: {src.name}"

    if fmt == "wav" or not embed_cover:
        return index, src, dst, True, f"    -> {dst.name}"

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        cover_file = get_cover_art(src, tmp_path, src.stem, cover_priority, mutagen_verbose)
        if cover_file:
            if add_cover_art_mutagen(dst, cover_file, fmt, mutagen_verbose):
                return index, src, dst, True, f"    -> {dst.name}"
            else:
                return index, src, dst, True, f"    -> {dst.name} (Cover FAIL)"
        else:
            return index, src, dst, True, f"    -> {dst.name} (No Cover)"


# directory filtering utils
def load_filter_preset() -> Set[str]:
    if FILTER_PRESET_FILE.exists():
        try:
            with open(FILTER_PRESET_FILE, "r") as f:
                data = json.load(f)
                return set(data.get("included_folders", []))
        except:
            return set()
    return set()


def save_filter_preset(included: Set[str]):
    with open(FILTER_PRESET_FILE, "w") as f:
        json.dump({"included_folders": list(included)}, f, indent=2)


def interactive_folder_filter(source_root: Path) -> Set[str]:
    included = set()
    current_path = source_root
    history: List[Path] = []

    print_header("Folder Filtering")
    print("Navigate, toggle inclusion, or apply patterns.")
    while True:
        if current_path == source_root:
            rel_current = "."
        else:
            rel_current = str(current_path.relative_to(source_root))
        print(f"\nCurrent location: {rel_current}")
        print("\nCommands:")
        print("    <number>        - toggle the listed folder (include/exclude)")
        print("    + <number>      - toggle the folder and ALL its subfolders recursively")
        print("    ..              - go up one level")
        print("    p               - apply pattern (e.g., *Tokimeki*)")
        print("    s               - save current filter set as preset")
        print("    l               - load filter preset")
        print("    c               - clear all filters")
        print("    d               - done")
        print("    q               - quit (use all folders)")
        print("    <folder name>   - change into that subdirectory") # I think I could've made this more intuitive but I'm fucking tired of writing this shit so I will not be doing that.

        subdirs = [d for d in current_path.iterdir() if d.is_dir() and not d.name.startswith('.')]
        subdirs.sort()
        if not subdirs:
            print("(No subdirectories found)")

        for i, d in enumerate(subdirs, 1):
            rel = str(d.relative_to(source_root))
            mark = "[X]" if rel in included else "[ ]"
            print(f"  {i:2}. {mark} {d.name}")

        choice = input("\n> ").strip()
        if choice == "":
            continue
        elif choice == "..":
            if history:
                current_path = history.pop()
            elif current_path != source_root:
                history.append(current_path)
                current_path = current_path.parent
            else:
                print("Already at root.")
        elif choice == "p":
            pattern = input("Enter pattern: ").strip()
            if pattern:
                matched = []
                for root, dirs, files in os.walk(source_root):
                    for d in dirs:
                        full = Path(root) / d
                        rel = str(full.relative_to(source_root))
                        if fnmatch.fnmatch(rel, pattern):
                            matched.append(rel)
                if not matched:
                    print("No folders match pattern.")
                else:
                    print(f"Found {len(matched)} matching folders.")
                    for m in matched[:20]:
                        print(f"  {m}")
                    if len(matched) > 20:
                        print(f"  ... and {len(matched)-20} more")
                    confirm = input("Add results? (y/n): ").strip().lower()
                    if confirm == "y":
                        included.update(matched)
                        print(f"Added {len(matched)} folders.")
        elif choice == "s":
            save_filter_preset(included)
            print("Filter preset saved.")
        elif choice == "l":
            loaded = load_filter_preset()
            if loaded:
                included = loaded
                print(f"Loaded {len(included)} folders from preset.")
            else:
                print("No preset found or preset is empty.")
        elif choice == "c":
            included.clear()
            print("All filters cleared.")
        elif choice == "d":
            break
        elif choice == "q":
            return set()
        elif choice.startswith("+") and len(choice.split()) == 2:
            # recursive toggle: + <number>
            parts = choice.split()
            idx = int(parts[1]) - 1
            if 0 <= idx < len(subdirs):
                target = subdirs[idx]
                rel_target = str(target.relative_to(source_root))
                if rel_target in included:
                    to_remove = {rel_target}
                    for root, dirs, files in os.walk(target):
                        for d in dirs:
                            full = Path(root) / d
                            rel = str(full.relative_to(source_root))
                            to_remove.add(rel)
                    included.difference_update(to_remove)
                    print(f"Removed {rel_target} and all its subfolders.")
                else:
                    to_add = {rel_target}
                    for root, dirs, files in os.walk(target):
                        for d in dirs:
                            full = Path(root) / d
                            rel = str(full.relative_to(source_root))
                            to_add.add(rel)
                    included.update(to_add)
                    print(f"Added {rel_target} and all its subfolders.")
            else:
                print("Invalid number.")
        elif choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(subdirs):
                rel = str(subdirs[idx].relative_to(source_root))
                if rel in included:
                    included.remove(rel)
                    print(f"Excluded {subdirs[idx].name}")
                else:
                    included.add(rel)
                    print(f"Included {subdirs[idx].name}")
            else:
                print("Invalid number.")
        else:
            try:
                new_path = current_path / choice
                if new_path.is_dir():
                    history.append(current_path)
                    current_path = new_path
                else:
                    print("Not a directory.")
            except:
                print("Invalid command.")
    return included


def find_flac_files(root: Path, exclude_dirs: List[str], include_set: Optional[Set[str]] = None) -> List[Path]:
    if include_set is None:
        include_set = set()
    flac_files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in exclude_dirs]
        rel = str(Path(dirpath).relative_to(root))
        if include_set and rel not in include_set:
            continue
        for f in filenames:
            if f.lower().endswith(".flac"):
                flac_files.append(Path(dirpath) / f)
    return flac_files


def group_files_by_album(flac_files: List[Path]) -> Dict[Path, List[Path]]:
    albums = defaultdict(list)
    for src in flac_files:
        albums[src.parent].append(src)
    for album in albums:
        albums[album].sort()
    return dict(albums)


def delete_files_and_cleanup(file_paths: List[Path]):
    for f in file_paths:
        if f.is_file():
            f.unlink()
    parents = {f.parent for f in file_paths}
    for parent in parents:
        if parent.exists() and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()


# deleter functions
def interactive_file_browser(root: Path) -> List[Path]:
    format_dirs = {"OPUS", "MP3", "AAC", "OGG", "WAV"}
    selected = []
    current = root

    while True:
        clear_screen()
        print("=" * 60)
        print("File Browser – Select files for deletion".center(60))
        print("=" * 60)
        if current == root:
            rel = "."
        else:
            rel = str(current.relative_to(root))
        print(f"\nCurrent: {rel}")
        print("\nCommands:")
        print("    <number>    - toggle the listed file or nav into folder")
        print("    ..          - go up one directory")
        print("    d           - delete currently selected files and exit")
        print("    q           - quit without deleting")
        print("    s           - show current selection")
        print("    enter       - refresh / list files\n")

        subdirs = [d for d in current.iterdir() if d.is_dir() and not d.name.startswith('.')]
        subdirs.sort()
        files_in_format = []
        for f in current.iterdir():
            if f.is_file() and f.parent.name.upper() in format_dirs:
                files_in_format.append(f)
        files_in_format.sort()

        if subdirs:
            print("Directories:")
            for i, d in enumerate(subdirs, 1):
                print(f"  {i:3}. [DIR]  {d.name}")
        else:
            print("(No subdirectories)")

        if files_in_format:
            print("\nFiles:")
            for i, f in enumerate(files_in_format, len(subdirs) + 1):
                mark = "[X]" if f in selected else "[ ]"
                size = f.stat().st_size / (1024 * 1024)
                print(f"  {i:3}. {mark} {f.name} ({size:.2f} MB)")
        else:
            print("\n(No deletable files in this folder)")

        choice = input("\n> ").strip()
        if choice == "":
            continue
        elif choice == "..":
            if current != root:
                current = current.parent
            else:
                print("Already at root.")
        elif choice == "d":
            if selected:
                print(f"\nDelete {len(selected)} file(s).")
                confirm = input("Confirm deletion? (y/n): ").strip().lower()
                if confirm == "y":
                    delete_files_and_cleanup(selected)
                    print("Deletion complete.")
                else:
                    print("Cancelled.")
            else:
                print("No files selected.")
            input("Press Enter to continue...")
            return selected
        elif choice == "q":
            return []
        elif choice == "s":
            if selected:
                print("\nCurrently selected:")
                for f in selected:
                    print(f"  {f.relative_to(root)}")
            else:
                print("No files selected.")
            input("Press Enter...")
        elif choice.isdigit():
            idx = int(choice) - 1
            if idx < len(subdirs):
                current = subdirs[idx]
            else:
                file_idx = idx - len(subdirs)
                if 0 <= file_idx < len(files_in_format):
                    f = files_in_format[file_idx]
                    if f in selected:
                        selected.remove(f)
                        print(f"Deselected {f.name}")
                    else:
                        selected.append(f)
                        print(f"Selected {f.name}")
                else:
                    print("Invalid number.")
        else:
            print("Unknown command.")


def interactive_deleter():
    print_header("Deleter")
    config = load_config()
    source_root = get_source_folder(config)
    if not source_root:
        return

    format_dirs = ["OPUS", "MP3", "AAC", "OGG", "WAV"]
    output_folders = []
    for root, dirs, files in os.walk(source_root):
        for d in dirs:
            if d.upper() in format_dirs:
                output_folders.append(Path(root) / d)

    if not output_folders:
        print("No formats found under the source.")
        input("Press Enter to return.")
        return

    print(f"Found {len(output_folders)} format folders.")
    print("Options:")
    print("    1. Delete all files in a specific format (and remove empty folders)")
    print("    2. Delete orphaned files (output files with no matching FLAC)")
    print("    3. Delete by album (choose album folder to delete all its output files)")
    print("    4. Delete specific file(s)")
    print("    5. Back")
    opt = input("\n> ").strip()

    if opt == "1":
        formats_present = set(of.name.upper() for of in output_folders)
        print("Found formats:", ", ".join(formats_present))
        fmt_choice = input("Enter format: ").strip().upper()
        if fmt_choice in formats_present:
            to_delete = []
            for of in output_folders:
                if of.name.upper() == fmt_choice:
                    for f in of.glob("*"):
                        if f.is_file():
                            to_delete.append(f)
            delete_files_and_cleanup(to_delete)
            print(f"Deleted {len(to_delete)} files in {fmt_choice} folders.")
        else:
            print("Format not found.")
    elif opt == "2":
        orphaned = []
        for of in output_folders:
            album_dir = of.parent
            for out_file in of.glob("*"):
                if out_file.is_file():
                    base = out_file.stem
                    flac_candidates = list(album_dir.glob(base + ".flac")) + list(album_dir.glob(base + ".*.flac"))
                    if not flac_candidates:
                        orphaned.append(out_file)
        if orphaned:
            print(f"Found {len(orphaned)} orphaned files.")
            confirm = input("Delete all? (y/n): ").strip().lower()
            if confirm == "y":
                delete_files_and_cleanup(orphaned)
                print("Deleted.")
            else:
                print("None deleted.")
        else:
            print("No orphaned files found.")
    elif opt == "3":
        albums = set(of.parent for of in output_folders)
        album_list = sorted(albums)
        print("Albums with converted files:")
        for i, alb in enumerate(album_list[:30], 1):
            print(f"  {i}. {alb.relative_to(source_root)}")
        if len(album_list) > 30:
            print(f"  ... and {len(album_list)-30} more")
        sel = input("Select album number (or 'all'): ").strip()
        if sel == "all":
            for alb in album_list:
                for fmt_dir in format_dirs:
                    d = alb / fmt_dir
                    if d.exists() and d.is_dir():
                        for f in d.glob("*"):
                            if f.is_file():
                                f.unlink()
                        if d.exists() and not any(d.iterdir()):
                            d.rmdir()
            print("Deleted all output files from all albums.")
        elif sel.isdigit():
            idx = int(sel) - 1
            if 0 <= idx < len(album_list):
                alb = album_list[idx]
                for fmt_dir in format_dirs:
                    d = alb / fmt_dir
                    if d.exists() and d.is_dir():
                        for f in d.glob("*"):
                            if f.is_file():
                                f.unlink()
                        if d.exists() and not any(d.iterdir()):
                            d.rmdir()
                print(f"Deleted output files from {alb.name}")
            else:
                print("Invalid number.")
    elif opt == "4":
        selected_files = interactive_file_browser(source_root)
        if selected_files:
            print(f"\nDeleted {len(selected_files)} file(s).")
        else:
            print("No files were deleted.")
    else:
        return
    input("\nPress Enter to continue.")


# Menus
def main_menu(config: Dict) -> str:
    while True:
        print_header("Main Menu (MuCa)") # MuCa - Multithreading Capable (The original is single threaded and therefore not sigma)
        print("\n1. Convert FLAC files")
        print("2. Settings")
        print("3. Interactive deleter")
        print("4. Exit")
        choice = input("\nChoose (1-4): ").strip()
        if choice in ("1", "2", "3", "4"):
            return choice
        print("Invalid choice.")


def format_settings_menu(fmt: str, config: Dict):
    while True:
        print_header(f"{fmt.upper()} Settings")
        if fmt == "opus":
            print(f"    Mode             : {config['opus_mode'].upper()}")
            if config["opus_mode"] == "vbr":
                print(f"    VBR bitrate      : {display_bitrate(config['opus_vbr_bitrate'])}")
            elif config["opus_mode"] == "cvbr":
                print(f"    CVBR bitrate     : {display_bitrate(config['opus_cvbr_bitrate'])}")
            else:
                print(f"    CBR bitrate      : {display_bitrate(config['opus_cbr_bitrate'])}")
            print(f"    Application      : {config['opus_application']}")
            print(f"    Extra options    : {config['opus_extra'] or '(none)'}")
            print("\n1. Change mode (VBR/CVBR/CBR)")
            print("2. Change bitrate")
            print("3. Change application type")
            print("4. Change extra ffmpeg options")
            print("5. Back")
            opt = input("\n> ").strip()
            if opt == "1":
                mode = input("Mode (vbr/cvbr/cbr) [vbr]: ").strip().lower() or "vbr"
                if mode in ("vbr", "cvbr", "cbr"):
                    config["opus_mode"] = mode
            elif opt == "2":
                mode = config["opus_mode"]
                if mode == "vbr":
                    br = input(f"VBR bitrate (e.g., 96k,128) [{config['opus_vbr_bitrate']}]: ").strip() or config["opus_vbr_bitrate"]
                    config["opus_vbr_bitrate"] = normalize_bitrate(br)
                elif mode == "cvbr":
                    br = input(f"CVBR bitrate (e.g., 96k,128) [{config['opus_cvbr_bitrate']}]: ").strip() or config["opus_cvbr_bitrate"]
                    config["opus_cvbr_bitrate"] = normalize_bitrate(br)
                else:
                    br = input(f"CBR bitrate (e.g., 96k,128) [{config['opus_cbr_bitrate']}]: ").strip() or config["opus_cbr_bitrate"]
                    config["opus_cbr_bitrate"] = normalize_bitrate(br)
            elif opt == "3":
                app = input("Application (audio/voip/lowdelay) [audio]: ").strip().lower() or "audio"
                if app in ("audio", "voip", "lowdelay"):
                    config["opus_application"] = app
            elif opt == "4":
                extra = input("Extra ffmpeg options (e.g., -compression_level 10): ").strip()
                config["opus_extra"] = extra
            elif opt == "5":
                break

        elif fmt == "mp3":
            print(f"    Mode             : {config['mp3_mode'].upper()}")
            if config["mp3_mode"] == "vbr":
                print(f"    VBR quality      : {config['mp3_vbr_quality']} (0=best,9=worst)")
            elif config["mp3_mode"] == "abr":
                print(f"    ABR bitrate      : {display_bitrate(config['mp3_abr_bitrate'])}")
            else:
                print(f"    CBR bitrate      : {display_bitrate(config['mp3_cbr_bitrate'])}")
            print(f"    Extra options    : {config['mp3_extra'] or '(none)'}")
            print("\n1. Change mode (VBR/ABR/CBR)")
            print("2. Change quality/bitrate")
            print("3. Change extra ffmpeg options")
            print("4. Back")
            opt = input("\n> ").strip()
            if opt == "1":
                mode = input("Mode (vbr/abr/cbr) [vbr]: ").strip().lower() or "vbr"
                if mode in ("vbr", "abr", "cbr"):
                    config["mp3_mode"] = mode
            elif opt == "2":
                mode = config["mp3_mode"]
                if mode == "vbr":
                    q = input(f"VBR quality (0-9) [{config['mp3_vbr_quality']}]: ").strip()
                    if q:
                        config["mp3_vbr_quality"] = q
                elif mode == "abr":
                    br = input(f"ABR bitrate (e.g., 192k,256) [{config['mp3_abr_bitrate']}]: ").strip()
                    if br:
                        config["mp3_abr_bitrate"] = normalize_bitrate(br)
                else:
                    br = input(f"CBR bitrate (e.g., 192k,320) [{config['mp3_cbr_bitrate']}]: ").strip()
                    if br:
                        config["mp3_cbr_bitrate"] = normalize_bitrate(br)
            elif opt == "3":
                extra = input("Extra ffmpeg options: ").strip()
                config["mp3_extra"] = extra
            elif opt == "4":
                break

        elif fmt == "aac":
            print(f"    Mode             : {config['aac_mode'].upper()}")
            if config["aac_mode"] == "vbr":
                print(f"    VBR quality      : {config['aac_vbr_quality']} (0=worst,2=best)")
            elif config["aac_mode"] == "abr":
                print(f"    ABR bitrate      : {display_bitrate(config['aac_abr_bitrate'])}")
            else:
                print(f"    CBR bitrate      : {display_bitrate(config['aac_cbr_bitrate'])}")
            print(f"    Extra options    : {config['aac_extra'] or '(none)'}")
            print("\n1. Change mode (VBR/ABR/CBR)")
            print("2. Change quality/bitrate")
            print("3. Change extra ffmpeg options")
            print("4. Back")
            opt = input("\n> ").strip()
            if opt == "1":
                mode = input("Mode (vbr/abr/cbr) [vbr]: ").strip().lower() or "vbr"
                if mode in ("vbr", "abr", "cbr"):
                    config["aac_mode"] = mode
            elif opt == "2":
                mode = config["aac_mode"]
                if mode == "vbr":
                    q = input(f"VBR quality (0-2) [{config['aac_vbr_quality']}]: ").strip()
                    if q:
                        config["aac_vbr_quality"] = q
                elif mode == "abr":
                    br = input(f"ABR bitrate (e.g., 192k,256) [{config['aac_abr_bitrate']}]: ").strip()
                    if br:
                        config["aac_abr_bitrate"] = normalize_bitrate(br)
                else:
                    br = input(f"CBR bitrate (e.g., 192k,256) [{config['aac_cbr_bitrate']}]: ").strip()
                    if br:
                        config["aac_cbr_bitrate"] = normalize_bitrate(br)
            elif opt == "3":
                extra = input("Extra ffmpeg options: ").strip()
                config["aac_extra"] = extra
            elif opt == "4":
                break

        elif fmt == "ogg":
            print(f"    Mode             : {config['ogg_mode'].upper()}")
            if config["ogg_mode"] == "vbr":
                print(f"    VBR quality      : {config['ogg_vbr_quality']} (-1=lowest,10=best)")
            elif config["ogg_mode"] == "abr":
                print(f"    ABR bitrate      : {display_bitrate(config['ogg_abr_bitrate'])}")
            else:
                print(f"    CBR bitrate      : {display_bitrate(config['ogg_cbr_bitrate'])}")
            print(f"    Extra options    : {config['ogg_extra'] or '(none)'}")
            print("\n1. Change mode (VBR/ABR/CBR)")
            print("2. Change quality/bitrate")
            print("3. Change extra ffmpeg options")
            print("4. Back")
            opt = input("\n> ").strip()
            if opt == "1":
                mode = input("Mode (vbr/abr/cbr) [vbr]: ").strip().lower() or "vbr"
                if mode in ("vbr", "abr", "cbr"):
                    config["ogg_mode"] = mode
            elif opt == "2":
                mode = config["ogg_mode"]
                if mode == "vbr":
                    q = input(f"VBR quality (-1 to 10) [{config['ogg_vbr_quality']}]: ").strip()
                    if q:
                        config["ogg_vbr_quality"] = q
                elif mode == "abr":
                    br = input(f"ABR bitrate (e.g., 192k,256) [{config['ogg_abr_bitrate']}]: ").strip()
                    if br:
                        config["ogg_abr_bitrate"] = normalize_bitrate(br)
                else:
                    br = input(f"CBR bitrate (e.g., 192k,320) [{config['ogg_cbr_bitrate']}]: ").strip()
                    if br:
                        config["ogg_cbr_bitrate"] = normalize_bitrate(br)
            elif opt == "3":
                extra = input("Extra ffmpeg options: ").strip()
                config["ogg_extra"] = extra
            elif opt == "4":
                break

        elif fmt == "wav":
            depth_display = config["wav_bit_depth"]
            if depth_display == "keep":
                depth_display = "keep (detect from FLAC)"
            print(f"    Bit depth        : {depth_display}")
            print(f"    Extra options    : {config['wav_extra'] or '(none)'}")
            print("\n1. Change bit depth (keep, 16, 24, 32)")
            print("2. Change extra ffmpeg options")
            print("3. Back")
            opt = input("\n> ").strip()
            if opt == "1":
                print("Options: keep, 16, 24, 32")
                depth = input("Bit depth [keep]: ").strip().lower() or "keep"
                if depth in ("keep", "16", "24", "32"):
                    config["wav_bit_depth"] = depth
                else:
                    print("Invalid option. Keeping previous.")
            elif opt == "2":
                extra = input("Extra ffmpeg options: ").strip()
                config["wav_extra"] = extra
            elif opt == "3":
                break

        save_config(config)
        input("\nSettings saved. Press Enter to continue...")


def general_settings_menu(config: Dict):
    while True:
        print_header("General Settings")
        print("-" * 40)
        print("Multithreading:")
        print(f"   1. CPU Percentage               : {config['cpu_usage_percent']}%")
        print(f"   2. Multithreaded Mode           : {'YES' if config['multithread'] else 'NO'}")
        print("-" * 40)
        print("Handling:")
        print(f"   3. Overwrite existing files     : {'YES' if config['overwrite'] else 'no'}")
        print(f"   4. FFmpeg verbose output        : {'YES' if config['ffmpeg_verbose'] else 'no'}")
        print(f"   5. Mutagen verbose output       : {'YES' if config['mutagen_verbose'] else 'no'}")
        print("-" * 40)
        print("Cover Art:")
        print(f"   6. Embed cover art (mutagen)    : {'YES' if config['embed_cover_art'] else 'NO'}")
        print(f"   7. Prioritise embedded cover    : {'YES (FLAC first)' if config['prioritize_embedded_cover'] else 'NO (cover.jpg first)'}")
        print("-" * 40)
        print("Audio:")
        print(f"   8. Sample rate                  : {config['sample_rate']}")
        print(f"   9. Channel handling (Opus)      : {config.get('channel_handling', 'auto')} (auto/stereo/keep)")
        print("-" * 40)
        print("10. Back to main menu")
        opt = input("\n> ").strip()
        if opt == "1":
            try:
                percent = int(input(f"CPU Percentage (1-100) [{config['cpu_usage_percent']}]: ").strip() or config["cpu_usage_percent"])
                config["cpu_usage_percent"] = max(1, min(100, percent))
            except:
                pass
        elif opt == "2":
            current = config["multithread"]
            val = input(f"Enable multithreaded mode? (y/n) [{'y' if current else 'n'}]: ").strip().lower()
            config["multithread"] = val == "y"
        elif opt == "3":
            config["overwrite"] = input("Overwrite existing files? (y/n) [n]: ").strip().lower() == "y"
        elif opt == "4":
            config["ffmpeg_verbose"] = input("Verbose ffmpeg output? (y/n) [n]: ").strip().lower() == "y"
        elif opt == "5":
            config["mutagen_verbose"] = input("Verbose mutagen cover art output? (y/n) [n]: ").strip().lower() == "y"
        elif opt == "6":
            config["embed_cover_art"] = input("Embed cover art using mutagen? (y/n) [y]: ").strip().lower() != "n"
        elif opt == "7":
            config["prioritize_embedded_cover"] = input("Prioritise embedded cover art over cover.jpg? (y/n) [y]: ").strip().lower() != "n"
        elif opt == "8":
            print("\nEnter sample rate: 'keep' (no change), a number (e.g., 44100), or with 'k' (e.g., 44.1k, 48k, 96k)")
            sr = input("Sample rate [keep]: ").strip()
            if sr == "":
                sr = "keep"
            normalized = normalize_sample_rate(sr)
            if normalized == "keep" and sr != "keep":
                print("Invalid sample rate, keeping previous value.")
                time.sleep(1)
            else:
                config["sample_rate"] = normalized
        elif opt == "9":
            print("\nChannel handling options:")
            print("  auto    – detect source channels and use proper Opus mapping (recommended)")
            print("  stereo  – force downmix to stereo")
            print("  keep    – do not add any -ac or -mapping_family (original behaviour)")
            ch = input("Channel handling [auto]: ").strip().lower() or "auto"
            if ch in ("auto", "stereo", "keep"):
                config["channel_handling"] = ch
            else:
                print("Invalid choice. Keeping previous value.")
        elif opt == "10":
            break
        save_config(config)
        input("\nSettings saved. Press Enter to continue...")


def settings_menu(config: Dict):
    while True:
        print_header("Settings – Select Format")
        print("1. Opus settings")
        print("2. MP3 settings")
        print("3. AAC settings")
        print("4. Ogg settings")
        print("5. WAV settings")
        print("6. General settings")
        print("7. Back to main menu")
        opt = input("\n> ").strip()
        if opt == "1":
            format_settings_menu("opus", config)
        elif opt == "2":
            format_settings_menu("mp3", config)
        elif opt == "3":
            format_settings_menu("aac", config)
        elif opt == "4":
            format_settings_menu("ogg", config)
        elif opt == "5":
            format_settings_menu("wav", config)
        elif opt == "6":
            general_settings_menu(config)
        elif opt == "7":
            break


def format_menu(config: Dict) -> List[str]:
    names = ["opus", "mp3", "aac", "ogg", "wav"]
    while True:
        print_header("Select Output Formats")
        print("(Toggle numbers, then Enter to save)\n")
        for i, name in enumerate(names, 1):
            mark = "[X]" if name in config["formats"] else "[ ]"
            print(f"    {i}. {mark} {name.upper()}")
        print("\n   Commands: 1,3  (toggle)   all   <Enter> to save")
        cmd = input("\n> ").strip().lower()
        if cmd == "":
            return config["formats"]
        if cmd == "all":
            config["formats"] = names.copy()
            save_config(config)
            continue
        toggled = []
        for part in cmd.split(","):
            part = part.strip()
            if part.isdigit():
                idx = int(part) - 1
                if 0 <= idx < len(names):
                    toggled.append(names[idx])
        for fmt in toggled:
            if fmt in config["formats"]:
                config["formats"].remove(fmt)
            else:
                config["formats"].append(fmt)
        save_config(config)


def get_source_folder(config: Dict) -> Path:
    print_header("Select Source Folder")
    last = config.get("last_source", "")
    has_default = bool(last.strip())
    if has_default:
        default_path = Path(last).expanduser().resolve()
        print(f"Current default: {default_path}")
        prompt = "\nEnter new path, 'cwd' (use current folder), or press Enter to keep: "
    else:
        prompt = "\nEnter a path, or type 'cwd' to use the current folder: "
    src = input(prompt).strip()
    if not src:
        if has_default:
            path = Path(last).expanduser().resolve()
        else:
            path = Path.cwd()
    else:
        src_lower = src.lower()
        if src_lower in ("cwd", "."):
            path = Path.cwd()
        else:
            path = Path(src).expanduser().resolve()
            if not path.is_dir():
                print(f"Error: '{path}' is not a directory. Using current directory.")
                path = Path.cwd()
    config["last_source"] = str(path)
    save_config(config)
    return path


# Converter
def run_conversion(config: Dict):
    print_header("Conversion (Multithreaded - Album Batching)")
    source_root = get_source_folder(config)

    use_filter = input("\nUse folder filtering? (y/n) [n]: ").strip().lower() == "y"
    included_folders = set()
    if use_filter:
        included_folders = interactive_folder_filter(source_root)
        if included_folders:
            print(f"Filter: {len(included_folders)} folders included.")
        else:
            print("No filter applied.")
    else:
        print("No filter, processing all files.")

    formats = format_menu(config)
    if not formats:
        print("No formats selected. Returning to main menu.")
        input("Press Enter...")
        return

    workers = compute_max_workers(config)
    multithread = config["multithread"]

    format_data = []
    for fmt in formats:
        # precompute folder output and extension
        if fmt == "wav":
            out_dir_name = "WAV"
            ext = ".wav"
        else:
            out_dir_name = fmt.upper()
            ext = {"opus": ".opus", "mp3": ".mp3", "aac": ".m4a", "ogg": ".ogg"}[fmt]
        format_data.append({
            "fmt": fmt,
            "out_dir_name": out_dir_name,
            "ext": ext,
        })

    exclude_dirs = [fd["out_dir_name"] for fd in format_data]
    print("\nScanning for FLAC files...")
    flac_files = find_flac_files(source_root, exclude_dirs, included_folders if included_folders else None)

    if not flac_files:
        print("No FLAC files found in selected folders.")
        input("Press Enter...")
        return

    albums = group_files_by_album(flac_files)
    total_flac = len(flac_files)
    total_output_files = total_flac * len(formats)
    total_albums = len(albums)

    print(f"\nFound {total_flac} FLAC files in {total_albums} album folders.")
    print(f"Selected formats: {', '.join(formats).upper()}")
    print(f"Will generate {total_output_files} output files.")
    print("\nSettings:")
    print(f"   Source: {source_root}")
    print(f"   Workers per album: {workers}")
    print(f"   Multithreaded Mode: {'YES' if multithread else 'NO'}")
    print(f"   Overwrite: {'yes' if config['overwrite'] else 'no'}")
    print(f"   FFmpeg verbose: {'yes' if config['ffmpeg_verbose'] else 'no'}")
    print(f"   Mutagen verbose: {'yes' if config['mutagen_verbose'] else 'no'}")
    print(f"   Embed cover art: {'yes' if config['embed_cover_art'] else 'NO'}")
    print(f"   Prioritise embedded cover: {'yes (FLAC first)' if config['prioritize_embedded_cover'] else 'no (cover.jpg first)'}")
    print(f"   Sample rate: {config['sample_rate']}")
    print(f"   Channel handling (Opus): {config.get('channel_handling', 'auto')}")

    confirm = input("\nStart conversion? (y/n) [y]: ").strip().lower()
    if confirm not in ("y", ""):
        print("Cancelled.")
        input("Press Enter to return.")
        return

    # Process formats sequentially
    for fd in format_data:
        fmt = fd["fmt"]
        out_dir_name = fd["out_dir_name"]
        ext = fd["ext"]
        # ffmpeg arguments for format (once per, same for all files)
        # need sample config and source file for bit depth detection (WAV).
        safe_print(f"\n=== Converting to {fmt.upper()} ===")
        total_success = 0
        for album_path, album_files in albums.items():
            safe_print(f"\nAlbum: {album_path}")
            tasks = []
            for file_idx, src in enumerate(album_files):
                dst_dir = src.parent / out_dir_name
                dst = dst_dir / (src.stem + ext)
                # make ff_args per file (WAV source detection, and channel detection)
                ff_args, desc = get_ffmpeg_args(fmt, config, config["sample_rate"], src)
                tasks.append((file_idx, src, dst, ff_args))

            if multithread:
                with ThreadPoolExecutor(max_workers=workers) as executor:
                    future_to_idx = {}
                    for idx, src, dst, ff_args in tasks:
                        future = executor.submit(
                            process_one_file,
                            idx, src, dst, fmt, ff_args,
                            config["prioritize_embedded_cover"],
                            config["embed_cover_art"],
                            config["mutagen_verbose"],
                            config["overwrite"],
                            config["ffmpeg_verbose"]
                        )
                        future_to_idx[future] = idx
                    for future in as_completed(future_to_idx):
                        idx = future_to_idx[future]
                        try:
                            result_idx, src, dst, success, msg = future.result()
                            safe_print(msg)
                            if success:
                                total_success += 1
                        except Exception as e:
                            safe_print(f"   Exception processing {src}: {e}")
            else:
                for idx, src, dst, ff_args in tasks:
                    _, _, _, success, msg = process_one_file(
                        idx, src, dst, fmt, ff_args,
                        config["prioritize_embedded_cover"],
                        config["embed_cover_art"],
                        config["mutagen_verbose"],
                        config["overwrite"],
                        config["ffmpeg_verbose"]
                    )
                    safe_print(msg)
                    if success:
                        total_success += 1
            safe_print(f"Album finished: {len(album_files)} files processed.")
        safe_print(f"\nFinished {fmt.upper()}: {total_success}/{total_flac} total processed.")
    input("\nConversion complete. Press Enter to return to main menu.")


def main():
    if not shutil.which("ffmpeg"):
        print("ERROR: ffmpeg not found in PATH.")
        input("Press Enter to exit...")
        sys.exit(1)

    config = load_config()
    if config["embed_cover_art"] and not check_mutagen():
        config["embed_cover_art"] = False
        save_config(config)
        print("Cover art embedding disabled.")
        time.sleep(2)

    while True:
        choice = main_menu(config)
        if choice == "1":
            run_conversion(config)
        elif choice == "2":
            settings_menu(config)
        elif choice == "3":
            interactive_deleter()
        elif choice == "4":
            print_header("Goodbye!")
            break


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nCancelled by user.")
    input("\nPress Enter to close...")