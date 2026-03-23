"""Load existing search_data.json into Profile objects."""

import json
import os

from v2.models import Profile, ProfileField, ProfileIdentity


# Map TLS fields to our type system
FIELD_TYPE_MAP = {
    "pitch": "self_reported",
    "problem": "self_reported",
    "solution": "self_reported",
    "uncertainties": "self_reported",
    "linkedin": "linkedin",
    "author_assessment": "expert_assessment",
    "recommendations": "expert_assessment",
    "category": "metadata",
    "decision": "metadata",
}

# Fields ordered by signal value — highest signal first so truncation
# doesn't cut the most important info
FIELD_PRIORITY = [
    "linkedin",
    "author_assessment",
    "recommendations",
    "pitch",
    "problem",
    "solution",
    "uncertainties",
]


def load_tls_profiles(path: str, dataset_id: str = "tls") -> list[Profile]:
    """Load search_data.json into Profile objects."""
    with open(path) as f:
        raw = json.load(f)

    profiles = []
    for entry in raw:
        identity = ProfileIdentity(
            name=entry.get("name"),
            linkedin_url=entry.get("linkedin_url") or None,
        )

        fields = {}
        for field_name, field_type in FIELD_TYPE_MAP.items():
            value = entry.get(field_name, "")
            if value and value.strip():
                fields[field_name] = ProfileField(value=value.strip(), type=field_type)

        profile = Profile(
            id=entry.get("id", ""),
            dataset_id=dataset_id,
            identity=identity,
            fields=fields,
        )
        profile.rebuild_raw_text(field_priority=FIELD_PRIORITY)
        profiles.append(profile)

    return profiles


def load_profiles(data_dir: str) -> list[Profile]:
    """Load all profiles from the data directory."""
    path = os.path.join(data_dir, "search_data.json")
    if not os.path.exists(path):
        return []
    return load_tls_profiles(path)


if __name__ == "__main__":
    import config
    profiles = load_profiles(config.DATA_DIR)
    print(f"Loaded {len(profiles)} profiles")
    if profiles:
        p = profiles[0]
        print(f"  First: {p.identity.name}")
        print(f"  Fields: {list(p.fields.keys())}")
        print(f"  raw_text length: {len(p.raw_text)} chars")
