from app.models.schemas import ModelList


def filter_model_names(available_models, whitelist_models, blocked_models):
    if whitelist_models:
        return [model for model in available_models if model in whitelist_models]
    return [model for model in available_models if model not in blocked_models]


def build_aistudio_model_list(available_models, whitelist_models, blocked_models):
    filtered_models = filter_model_names(available_models, whitelist_models, blocked_models)

    return ModelList(
        data=[
            {
                "id": model,
                "object": "model",
                "created": 1678888888,
                "owned_by": "organization-owner",
            }
            for model in filtered_models
        ]
    )


def build_claude_model_list(
    available_models,
    whitelist_models,
    blocked_models,
    aliases=None,
    default_model="",
):
    filtered_models = filter_model_names(available_models, whitelist_models, blocked_models)
    aliases = aliases if isinstance(aliases, dict) else {}
    alias_models = [
        alias
        for alias, target in aliases.items()
        if "*" not in alias and (not target or target in filtered_models)
    ]
    if default_model and default_model in filtered_models and not alias_models:
        alias_models.append(default_model)
    model_ids = alias_models or filtered_models
    return {
        "data": [
            {
                "type": "model",
                "id": model,
                "display_name": model,
                "created_at": "2024-01-01T00:00:00Z",
            }
            for model in model_ids
        ],
        "has_more": False,
        "first_id": model_ids[0] if model_ids else None,
        "last_id": model_ids[-1] if model_ids else None,
    }
