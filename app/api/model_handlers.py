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


def build_claude_model_list(available_models, whitelist_models, blocked_models):
    filtered_models = filter_model_names(available_models, whitelist_models, blocked_models)
    return {
        "data": [
            {
                "type": "model",
                "id": model,
                "display_name": model,
                "created_at": "2024-01-01T00:00:00Z",
            }
            for model in filtered_models
        ],
        "has_more": False,
        "first_id": filtered_models[0] if filtered_models else None,
        "last_id": filtered_models[-1] if filtered_models else None,
    }
