"""Consumer validation for Prompt Writer visual_observations_v1."""
import math
import re

FIELDS = {"environment", "shot_type", "framing", "posture", "gaze", "gestures",
          "expression", "lighting", "props", "continuity", "acting", "camera_movement"}


def validate_observation(value, ids):
    if not isinstance(value, dict) or set(value) != {"version", "evidence_ids", "observations", "subjects", "confidence"}:
        raise ValueError("Invalid VLM observation schema")
    if value["version"] != 1 or value["evidence_ids"] != ids:
        raise ValueError("VLM observations do not match the selected evidence")
    confidence = value["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (float, int)) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ValueError("Invalid VLM confidence")
    observations = value["observations"]
    if not isinstance(observations, dict) or set(observations) != FIELDS:
        raise ValueError("Invalid VLM observation fields")
    subjects = value["subjects"]
    if not isinstance(subjects, list) or len(subjects) > 12:
        raise ValueError("Invalid VLM subject list")
    for text in [*observations.values(), *subjects]:
        if text is not None and (not isinstance(text, str) or len(text) > 1200 or re.search(r"[<>]|\[\d|\b(?:S\d+|Picture\s+\d+)\b", text)):
            raise ValueError("VLM descriptions contain unsupported prompt markup")
    if len(ids) < 2 and observations["camera_movement"] is not None:
        raise ValueError("A still image cannot establish camera movement")
    return value


def observation_description(value):
    # Stable field order, visual prose only. No dialogue or raw JSON enters H3.
    return "\n".join(f"{key.replace('_', ' ').capitalize()}: {value['observations'][key]}"
                     for key in ["environment", "shot_type", "framing", "posture", "gaze", "gestures", "expression",
                                 "lighting", "props", "continuity", "acting", "camera_movement"] if value["observations"][key])
