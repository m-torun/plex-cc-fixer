import json
import subprocess


def audio_stream_index(path, preferred=None):
    if preferred is not None:
        return preferred
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=index:stream_tags=language", "-of", "json", path],
        capture_output=True, text=True, check=True).stdout
    streams = json.loads(out).get("streams", [])
    if not streams:
        raise SystemExit(f"no audio streams in {path}")
    for stream in streams:
        if stream.get("tags", {}).get("language") == "eng":
            return stream["index"]
    return streams[0]["index"]


def extract_audio(src, dst, stream_index=None, start=None, duration=None):
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", src]
    if start is not None:
        cmd += ["-ss", str(start)]
    if duration is not None:
        cmd += ["-t", str(duration)]
    if stream_index is not None:
        cmd += ["-map", f"0:{stream_index}"]
    cmd += ["-vn", "-ac", "1", "-ar", "16000", dst]
    subprocess.run(cmd, check=True)


def extract_captions(workdir, src_name="source.ts", dst_name="captions.srt"):
    """Decode the EIA-608 captions embedded in the video stream. Runs in workdir so the
    source path needs no escaping inside the ffmpeg filter graph."""
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
           "-i", f"movie={src_name}[out0+subcc]", "-map", "0:1", "-c:s", "srt", dst_name]
    subprocess.run(cmd, cwd=workdir, check=True)
