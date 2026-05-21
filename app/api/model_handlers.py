from app.models.schemas import ModelList


def build_aistudio_model_list(available_models, whitelist_models, blocked_models):
    if whitelist_models:
        filtered_models = [model for model in available_models if model in whitelist_models]
    else:
        filtered_models = [model for model in available_models if model not in blocked_models]

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
