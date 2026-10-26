"""Exercise actual SDK wire serialization with a local HTTP transport, no paid calls."""
import base64
import json

import httpx
from google import genai

from app.vertex.api_helpers import create_generation_config
from app.vertex.message_processing import create_gemini_prompt, convert_to_openai_format
from app.vertex.models import OpenAIMessage, OpenAIRequest


async def run():
    signature = base64.b64encode(b"opaque-provider-signature").decode()
    received = []
    def transport(request):
        received.append(json.loads(request.content))
        return httpx.Response(200, json={
            "candidates": [{"content": {"role": "model", "parts": [
                {"functionCall": {"id": "next-id", "name": "lookup", "args": {"q": "ok"}}, "thoughtSignature": signature},
            ]}, "finishReason": "STOP"}],
        })
    client = genai.Client(vertexai=True, api_key='test-key', http_options={
        'client_args': {'trust_env': False},
        'async_client_args': {'transport': httpx.MockTransport(transport), 'trust_env': False},
    })
    request = OpenAIRequest(model='gemini-2.5-flash', messages=[
        OpenAIMessage(role='assistant', tool_calls=[{
            'id': 'previous-id', 'type': 'function', 'function': {'name': 'lookup', 'arguments': '{"q":"old"}'},
            'extra_content': {'google': {'thought_signature': signature}},
        }]),
        OpenAIMessage(role='tool', tool_call_id='previous-id', content='{"result":"ok"}'),
    ], tools=[{'type': 'function', 'function': {
        'name': 'lookup', 'parameters': {'type': 'object', 'properties': {'q': {'type': 'string'}}, 'required': ['q']},
    }}], tool_choice='required', response_format={'type': 'json_schema', 'json_schema': {'name': 'result', 'schema': {'type': 'object'}}})
    try:
        response = await client.aio.models.generate_content(
            model=request.model, contents=create_gemini_prompt(request.messages),
            config=create_generation_config(request),
        )
        assert len(received) == 1  # SDK must never automatically execute/replay tools.
        wire = received[0]
        assert wire['contents'][0]['parts'][0]['thought_signature'] == signature
        assert wire['contents'][0]['parts'][0]['function_call']['id'] == 'previous-id'
        assert wire['contents'][1]['parts'][0]['function_response']['id'] == 'previous-id'
        assert wire['tools'][0]['functionDeclarations'][0]['parametersJsonSchema']['required'] == ['q']
        assert wire['toolConfig']['function_calling_config']['mode'] == 'ANY'
        assert wire['generationConfig']['responseJsonSchema'] == {'type': 'object'}
        chat = convert_to_openai_format(response, request.model)
        assert chat['choices'][0]['message']['tool_calls'][0]['extra_content']['google']['thought_signature'] == signature
    finally:
        await client.aio.aclose()
        client.close()
