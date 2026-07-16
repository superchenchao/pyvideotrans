import json

import pytest

from videotrans.process.series_speakers import (
    character_voice,
    character_display_name,
    detach_episode_speaker,
    empty_manifest,
    infer_oversegmentation_speaker_count,
    load_manifest,
    manifest_path_for_series,
    merge_characters,
    register_episode,
    save_manifest,
    set_character_name,
    set_character_voice,
    suggest_character_names,
    voice_scope_key,
)


def _profile(*values, samples=3):
    return {"embedding": list(values), "sample_count": samples}


def test_cross_episode_matching_creates_stable_character_ids():
    manifest = empty_manifest("D:/drama")
    first = register_episode(
        manifest,
        episode_key="01.mp4",
        episode_name="01.mp4",
        speaker_profiles={
            "spk0": _profile(1.0, 0.0),
            "spk1": _profile(0.0, 1.0),
        },
        speaker_counts={"spk0": 8, "spk1": 3},
    )
    second = register_episode(
        manifest,
        episode_key="02.mp4",
        episode_name="02.mp4",
        speaker_profiles={
            "spk7": _profile(0.99, 0.04),
            "spk2": _profile(0.03, 0.99),
        },
        speaker_counts={"spk7": 5, "spk2": 6},
    )

    assert second["spk7"]["character_id"] == first["spk0"]["character_id"]
    assert second["spk2"]["character_id"] == first["spk1"]["character_id"]
    assert second["spk7"]["match_state"] == "matched"
    assert len(manifest["characters"]) == 2


def test_low_confidence_speaker_is_not_silently_merged():
    manifest = empty_manifest()
    register_episode(
        manifest,
        episode_key="01.mp4",
        episode_name="01.mp4",
        speaker_profiles={"spk0": _profile(1.0, 0.0, 0.0)},
    )
    result = register_episode(
        manifest,
        episode_key="02.mp4",
        episode_name="02.mp4",
        speaker_profiles={"spk0": _profile(0.0, 1.0, 0.0)},
    )

    assert result["spk0"]["match_state"] == "new"
    assert len(manifest["characters"]) == 2


def test_two_speakers_in_same_episode_cannot_collapse_to_one_character():
    manifest = empty_manifest()
    first = register_episode(
        manifest,
        episode_key="01.mp4",
        episode_name="01.mp4",
        speaker_profiles={"spk0": _profile(1.0, 0.0)},
    )
    result = register_episode(
        manifest,
        episode_key="02.mp4",
        episode_name="02.mp4",
        speaker_profiles={
            "spk0": _profile(0.99, 0.02),
            "spk1": _profile(0.98, 0.03),
        },
    )

    ids = {item["character_id"] for item in result.values()}
    assert len(ids) == 2
    assert first["spk0"]["character_id"] in ids


def test_confirmed_name_and_voice_persist_across_episode_match():
    manifest = empty_manifest()
    first = register_episode(
        manifest,
        episode_key="01.mp4",
        episode_name="01.mp4",
        speaker_profiles={"spk0": _profile(1.0, 0.0)},
    )
    character_id = first["spk0"]["character_id"]
    set_character_name(manifest, character_id, "沈轻")
    set_character_voice(manifest, character_id, "Xiaoyu")

    second = register_episode(
        manifest,
        episode_key="02.mp4",
        episode_name="02.mp4",
        speaker_profiles={"spk4": _profile(0.99, 0.01)},
    )
    character = next(item for item in manifest["characters"] if item["id"] == character_id)

    assert second["spk4"]["character_id"] == character_id
    assert character_display_name(character) == "沈轻"
    assert character["name_confirmed"] is True
    assert character["voice"] == "Xiaoyu"


def test_character_voices_are_isolated_by_tts_and_target_language():
    manifest = empty_manifest()
    mapping = register_episode(
        manifest,
        episode_key="01.mp4",
        episode_name="01.mp4",
        speaker_profiles={"spk0": _profile(1.0, 0.0)},
    )
    character_id = mapping["spk0"]["character_id"]
    ja_scope = voice_scope_key(0, "ja")
    es_scope = voice_scope_key(28, "es")
    set_character_voice(manifest, character_id, "Nanami", scope=ja_scope)
    set_character_voice(manifest, character_id, "Elena", scope=es_scope)
    character = manifest["characters"][0]

    assert character_voice(character, ja_scope) == "Nanami"
    assert character_voice(character, es_scope) == "Elena"


def test_auto_voice_can_be_corrected_but_manual_voice_is_preserved():
    manifest = empty_manifest()
    scope = voice_scope_key(28, "ja")
    mapping = register_episode(
        manifest,
        episode_key="01.mp4",
        episode_name="01.mp4",
        speaker_profiles={"spk0": _profile(1.0, 0.0)},
        speaker_to_voice={"spk0": "WrongMale"},
        voice_scope=scope,
    )
    character_id = mapping["spk0"]["character_id"]

    register_episode(
        manifest,
        episode_key="01.mp4",
        episode_name="01.mp4",
        speaker_profiles={"spk0": _profile(1.0, 0.0)},
        speaker_to_voice={"spk0": "CorrectFemale"},
        voice_scope=scope,
    )
    character = manifest["characters"][0]
    assert character_voice(character, scope) == "CorrectFemale"
    assert character["voice_sources"][scope] == "auto"

    set_character_voice(
        manifest, character_id, "ChosenByUser", scope=scope
    )
    register_episode(
        manifest,
        episode_key="01.mp4",
        episode_name="01.mp4",
        speaker_profiles={"spk0": _profile(1.0, 0.0)},
        speaker_to_voice={"spk0": "AnotherAutoVoice"},
        voice_scope=scope,
    )
    assert character_voice(character, scope) == "ChosenByUser"
    assert character["voice_sources"][scope] == "manual"


def test_name_suggestions_are_conservative_and_unconfirmed():
    subtitles = [
        {"line": 1, "text": "我叫沈轻"},
        {"line": 2, "text": "王大成！"},
        {"line": 3, "text": "你找我？"},
        {"line": 4, "text": "妈妈！"},
        {"line": 5, "text": "怎么了"},
    ]
    suggestions = suggest_character_names(
        subtitles, ["spk0", "spk0", "spk1", "spk1", "spk2"]
    )

    assert suggestions["spk0"][0]["name"] == "沈轻"
    assert suggestions["spk0"][0]["score"] == pytest.approx(0.98)
    assert suggestions["spk1"][0]["name"] == "王大成"
    assert "spk2" not in suggestions


def test_manifest_is_saved_atomically_and_invalid_json_falls_back(tmp_path):
    path = tmp_path / "series_characters.json"
    manifest = empty_manifest("D:/drama")
    save_manifest(path, manifest)

    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1
    path.write_text("not-json", encoding="utf-8")
    assert load_manifest(path, series_folder="D:/drama")["characters"] == []


def test_different_source_folders_do_not_share_one_series_manifest(tmp_path):
    first = manifest_path_for_series(tmp_path, "D:/drama-a")
    second = manifest_path_for_series(tmp_path, "D:/drama-b")

    assert first != second
    assert first.parent == tmp_path


def test_different_selected_batches_in_same_folder_do_not_share_manifest(tmp_path):
    first = manifest_path_for_series(
        tmp_path, "D:/drama", ["D:/drama/01.mp4"]
    )
    second = manifest_path_for_series(
        tmp_path, "D:/drama", ["D:/drama/01.mp4", "D:/drama/02.mp4"]
    )

    assert first != second


def test_reopening_new_character_with_null_similarity_is_supported():
    manifest = empty_manifest()
    first = register_episode(
        manifest,
        episode_key="01.mp4",
        episode_name="01.mp4",
        speaker_profiles={"spk0": _profile(1.0, 0.0)},
    )
    reopened = register_episode(
        manifest,
        episode_key="01.mp4",
        episode_name="01.mp4",
        speaker_profiles={"spk0": _profile(1.0, 0.0)},
    )

    assert reopened["spk0"]["character_id"] == first["spk0"]["character_id"]
    assert reopened["spk0"]["similarity"] is None
    assert reopened["spk0"]["match_state"] == "existing"


def test_extreme_unlimited_oversegmentation_is_retried_with_dominant_count():
    labels = (
        ["spk0"] * 10 + ["spk1"] * 9 + ["spk2"] * 7 + ["spk3"] * 6
        + [f"spk{index}" for index in range(4, 30) for _ in range(2)]
    )

    assert infer_oversegmentation_speaker_count(labels) == 4
    assert infer_oversegmentation_speaker_count(
        ["spk0"] * 30 + ["spk1"] * 25 + ["spk2"] * 10
    ) is None


def test_predicate_after_wo_shi_is_not_suggested_as_a_name():
    suggestions = suggest_character_names(
        [{"line": 1, "text": "我是多余的"}], ["spk0"]
    )

    assert suggestions == {}


def test_manual_merge_updates_episode_mappings():
    manifest = empty_manifest()
    first = register_episode(
        manifest,
        episode_key="01.mp4",
        episode_name="01.mp4",
        speaker_profiles={"spk0": _profile(1.0, 0.0)},
    )
    second = register_episode(
        manifest,
        episode_key="02.mp4",
        episode_name="02.mp4",
        speaker_profiles={"spk9": _profile(0.0, 1.0)},
    )
    target = first["spk0"]["character_id"]
    source = second["spk9"]["character_id"]

    merge_characters(manifest, target, [source])

    assert len(manifest["characters"]) == 1
    assert manifest["episodes"]["02.mp4"]["speakers"]["spk9"]["character_id"] == target


def test_manual_merge_rejects_two_speakers_from_same_episode():
    manifest = empty_manifest()
    mapping = register_episode(
        manifest,
        episode_key="01.mp4",
        episode_name="01.mp4",
        speaker_profiles={
            "spk0": _profile(1.0, 0.0),
            "spk1": _profile(0.0, 1.0),
        },
    )

    with pytest.raises(ValueError, match="同一集"):
        merge_characters(
            manifest,
            mapping["spk0"]["character_id"],
            [mapping["spk1"]["character_id"]],
        )


def test_manual_split_recovers_from_false_cross_episode_match():
    manifest = empty_manifest()
    first = register_episode(
        manifest,
        episode_key="01.mp4",
        episode_name="01.mp4",
        speaker_profiles={"spk0": _profile(1.0, 0.0)},
    )
    register_episode(
        manifest,
        episode_key="02.mp4",
        episode_name="02.mp4",
        speaker_profiles={"spk7": _profile(0.99, 0.01)},
    )
    old_id = first["spk0"]["character_id"]

    new_character = detach_episode_speaker(manifest, "02.mp4", "spk7")

    assert new_character["id"] != old_id
    assert manifest["episodes"]["02.mp4"]["speakers"]["spk7"]["character_id"] == new_character["id"]
    assert "02.mp4" not in next(
        item for item in manifest["characters"] if item["id"] == old_id
    )["episodes"]
