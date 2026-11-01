"""`better-captions run` argument parsing: AUDIO, --fixed-track and --track ROLE:NAME=PATH."""

import pytest

from better_captions import cli
from better_captions.engine.fixed_tracks import FixedTrack
from better_captions.engine.multitrack import GROUP, NONE, PERSON, Track


def _parse(*args):
    return cli.build_parser().parse_args(["run", "--out", "o", *args])


def test_track_options_parse_into_roles_names_and_paths():
    args = _parse("--track", "person:Anthony=mic.wav", "--track", "group:Discord=discord.wav", "--track", "none=g.wav")
    assert args.audio is None
    assert args.track == [
        Track(PERSON, "Anthony", "mic.wav"),
        Track(GROUP, "Discord", "discord.wav"),
        Track(NONE, "", "g.wav"),
    ]
    assert _parse("mix.wav").track == []


@pytest.mark.parametrize("spec", ["robot:R2=r.wav", "person:=mic.wav", "group:Discord", "mic.wav"])
def test_bad_track_specs_are_rejected(spec, capsys):
    with pytest.raises(SystemExit) as exc:
        _parse("--track", spec)
    assert exc.value.code == 2
    assert "--track" in capsys.readouterr().err


@pytest.fixture
def captured(monkeypatch):
    seen = {}

    def run(audio, out, options):
        seen.update(audio=audio, out=out, options=options)

    monkeypatch.setattr(cli, "run", run)
    return seen


def test_tracks_run_without_audio(captured):
    assert cli.main(["run", "--out", "o", "--track", "person:Anthony=mic.wav", "--track", "group=d.wav"]) == 0
    assert captured["audio"] is None
    assert captured["options"].tracks == [Track(PERSON, "Anthony", "mic.wav"), Track(GROUP, "", "d.wav")]
    assert captured["options"].fixed_tracks == []


def test_a_fixed_track_with_the_mix_keeps_the_mix_path(captured):
    assert cli.main(["run", "mix.wav", "--out", "o", "--fixed-track", "Anthony=mic.wav"]) == 0
    assert captured["audio"] == "mix.wav"
    assert captured["options"].fixed_tracks == [FixedTrack("Anthony", "mic.wav")]
    assert captured["options"].tracks == []


def test_a_fixed_track_without_the_mix_is_a_person_track(captured):
    assert cli.main(["run", "--out", "o", "--fixed-track", "Anthony=mic.wav", "--track", "group:Discord=d.wav"]) == 0
    assert captured["audio"] is None
    assert captured["options"].tracks == [Track(PERSON, "Anthony", "mic.wav"), Track(GROUP, "Discord", "d.wav")]
    assert captured["options"].fixed_tracks == []


@pytest.mark.parametrize(
    "argv",
    [["run", "--out", "o"], ["run", "mix.wav", "--out", "o", "--track", "group=d.wav"]],
    ids=["neither", "both"],
)
def test_audio_and_tracks_exclude_each_other(argv, captured, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2 and captured == {}
    assert "AUDIO" in capsys.readouterr().err
