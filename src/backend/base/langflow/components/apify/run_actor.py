import json
import string
from typing import Any, Callable, cast

from apify_client import ApifyClient
from langchain_community.document_loaders.apify_dataset import ApifyDatasetLoader
from langchain_core.callbacks import CallbackManagerForToolRun
from langchain_core.tools import BaseTool
from langflow.inputs.inputs import BoolInput
from pydantic import BaseModel, Field

from langflow.base.langchain_utilities.model import LCToolComponent
from langflow.field_typing import Tool
from langflow.io import MultilineInput, SecretStrInput, StrInput, Output
from langflow.schema import Data, dotdict
from langchain_core.documents import Document


class ApifyRunActorComponent(LCToolComponent):
    display_name = "Apify Actors"
    description = ("Use Apify actors in your flow to accomplish various tasks. "
        "This component can be used in a flow to retrieve data or as a tool with an agent.")
    documentation: str = "http://docs.langflow.org/components/apify/run-actor"
    icon = "Apify"
    name = "ApifyRunActor"
    beta = True

    inputs = [
        SecretStrInput(
            name="apify_token",
            display_name="Apify Token",
            info="The API token for the Apify account.",
            required=True,
            password=True,
        ),
        StrInput(
            name="actor_id",
            display_name="Actor",
            info="Actor name from Apify store to run. For example 'apify/website-content-crawler'.",
            required=True,
        ),
        # multiline input is more pleasant to use than the nested dict input
        MultilineInput(
            name="actor_input",
            display_name="Actor input",
            info="The JSON input for the actor.",
            value="{}",
            required=True,
        ),
        MultilineInput(
            name="dataset_fields",
            display_name="Output fields",
            info=("Fields to extract from the dataset, split by commas. "
                "Other fields will be ignored. Dots in nested structure will be replaced by undescrore. "
                "Sample input: 'field1, metadata.field2'. "
                "Sample output: {'field1': 1, 'metadata_field2': 2}. "
                "For example, for the 'apify/website-content-crawler' Actor, you can extract the 'markdown' field, "
                "which is the content of the website in markdown format."),
        ),
        BoolInput(
            name="do_flatten_dataset",
            display_name="Flatten output?",
            info=("The output dataset will be converted from a nested format to a flat structure. "
                "Dot in nested structure will be replaced by underscore. "
                "This is useful for further processing the of the Data object. "
                "For example, {'a': {'b': 1}} will be flattened to {'a_b': 1}."),
        ),
    ]

    outputs = [
        Output(display_name="Output", name="output", method="run_model"),
        Output(display_name="Tool", name="tool", method="build_tool"),
    ]

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._apify_client: ApifyClient | None = None

    def run_model(self) -> list[Data]:
        """Run the actor and return node output."""
        input = json.loads(self.actor_input)
        fields = self._parse_dataset_fields(self.dataset_fields) if self.dataset_fields else None
        res = self._run_actor(self.actor_id, input, fields=fields)
        if self.do_flatten_dataset:
            res = [self._flatten(item) for item in res]
        data = [Data(data=item) for item in res]

        self.status = data
        return data

    def build_tool(self) -> Tool:
        """Build a tool for agent that runs the Apify actor."""
        client = self._get_apify_client()
        actor_id = self.actor_id
        _run_actor = lambda actor_id, input: self._run_actor(actor_id, input)

        build = self._get_actor_latest_build(actor_id)
        readme = build.get("readme", "")[:250] + "..."
        properties, required = self._get_actor_input_schema_from_build(build)
        properties = {"actor_input": properties}

        # works from input schema
        info = [(
            "JSON encoded as string with input schema (STRICTLY FOLLOW JSON FORMAT AND SCHEMA):\n\n"
            f"{json.dumps(properties, separators=(',', ":"))}"
        )]
        if required:
            info.append("\n\nRequired fields:\n" + "\n".join(required))

        info = "".join(info)

        input_model_cls = self._create_input_model_class(info)
        tool_cls = self._create_tool_class(self, readme, input_model_cls, actor_id)

        return cast("Tool", tool_cls())

    def _create_tool_class(self, parent: 'ApifyRunActorComponent', readme: str,
                            input_model: type[BaseModel], actor_id: str) -> type[BaseTool]:
        """Create a tool class that runs an Apify actor."""
        class ApifyActorRun(BaseTool):
            """Tool that runs Apify actors."""

            name: str = f"apify_actor_{parent._toolify_actor_id_str(actor_id)}"
            description: str = (
                "Run an Apify actor with the given input."
                "Here is part of the currently loaded actor README:\n\n"
                f"{readme}\n\n"
            )

            args_schema: type[BaseModel] = input_model

            def _run(
                self,
                actor_input: str | dict,
                run_manager: CallbackManagerForToolRun | None = None,
            ) -> str:
                """Use the Apify actor."""
                if isinstance(actor_input, str):
                    input_dict = json.loads(actor_input)
                else:
                    input_dict = actor_input

                # retrieve if nested, just in case
                input_dict = input_dict.get("actor_input", input_dict)

                res = parent._run_actor(actor_id, input_dict)
                return '\n\n'.join([parent._dict_to_json_str(item) for item in res])

        return ApifyActorRun

    def _create_input_model_class(self, description: str) -> type[BaseModel]:
        """Create a Pydantic model class for the actor input."""
        class ActorInput(BaseModel):
            """Input for the Apify Actor tool."""

            actor_input: str = Field(..., description=description)

        return ActorInput

    def _get_apify_client(self) -> ApifyClient:
        """Get the Apify client. Is created if not exists or token changes."""
        if not self.apify_token:
            raise ValueError("API token is required.")
        # when token changes, create a new client
        if self._apify_client is None or self._apify_client.token != self.apify_token:
            self._apify_client = ApifyClient(self.apify_token)
        return self._apify_client

    def _get_actor_latest_build(self, actor_id: str) -> dict:
        """Get the latest build of an actor from default build tag."""
        client = self._get_apify_client()
        actor = client.actor(actor_id=actor_id)
        if not (actor_info := actor.get()):
            raise ValueError("Actor not found")

        default_build_tag = actor_info.get("defaultRunOptions", {}).get("build")
        latest_build_id = actor_info.get("taggedBuilds", {}).get(default_build_tag, {}).get("buildId")

        if (build := client.build(latest_build_id).get()) is None:
            raise ValueError("Build not found")

        return build

    def _get_actor_input_schema_from_build(self, build: dict) -> tuple[dict, list[str]]:
        """Get the input schema from the actor build.

        Trim the description to 250 characters.
        """
        if (input_schema_str := build.get("inputSchema")) is None:
            raise ValueError("Input schema not found")

        input_schema = json.loads(input_schema_str)

        MAX_DESCRIPTION_LEN = 250

        properties = input_schema.get("properties", {})
        required = input_schema.get("required", [])

        properties = {
            key: {
                "description": (value.get("description", "")[:MAX_DESCRIPTION_LEN] + "...")\
                                if len(value.get("description", "")) > MAX_DESCRIPTION_LEN else value.get("description", ""),
                "type": value.get("type"),
                "default": value.get("default"),
                "prefill": value.get("prefill"),
                "enum": value.get("enum"),
            }
            for key, value in properties.items()
        }

        # properties remove empty and None values
        to_delete = []
        for item in properties:
            for k, v in properties[item].items():
                if not v:
                    #del properties[item][k]
                    to_delete.append((item, k))
        for item, k in to_delete:
            del properties[item][k]

        return properties, required

    def _get_run_dataset_id(self, run_id: str) -> str:
        """Get the dataset id from the run id."""
        client = self._get_apify_client()
        run = client.run(run_id=run_id)
        if (dataset := run.dataset().get()) is None:
            raise ValueError("Dataset not found")
        if (did := dataset.get("id")) is None:
            raise ValueError("Dataset not found")
        return did

    def _dict_to_json_str(self, d: dict) -> str:
        """Convert a dictionary to a JSON string."""
        return json.dumps(d, separators=(',', ":"), default=lambda o: '<n/a>')

    def _toolify_actor_id_str(self, actor_id: str) -> str:
        """
        Turn actor_id into a valid tool name.

        Tool name must only contain letters, numbers, underscores, dashes,
        and cannot contain spaces.
        """
        allowed_special_chars = "_-"
        valid_chars = string.ascii_letters + string.digits + allowed_special_chars
        return ''.join(char if char in valid_chars else '_' for char in actor_id)

    def _run_actor(self, actor_id: str, input: dict,
        fields: list[str] | None = None) -> list[dict]:
        """
        Run an Apify actor and return the output dataset.

        :param actor_id: Actor name from Apify store to run.
        :param input: JSON input for the actor.
        :param fields: List of fields to extract from the dataset. Other fields will be ignored.
        """
        client = self._get_apify_client()
        if (details := client.actor(actor_id=actor_id).call(run_input=input)) is None:
            raise ValueError("Actor run details not found")
        if (run_id := details.get('id')) is None:
            raise ValueError("Run id not found")
        dataset_id = self._get_run_dataset_id(run_id)

        loader = ApifyDatasetLoader(
            dataset_id=dataset_id,
            dataset_mapping_function=lambda item: item if not fields else {
                k.replace(".", "_"): self._get_nested_value(item, k) for k in fields
            }
        )
        return loader.load()

    def _get_nested_value(self, data: dict[str, Any], key: str) -> Any:
        """Get a nested value from a dictionary."""
        keys = key.split('.')
        value = data
        for key in keys:
            if not isinstance(value, dict) or key not in value:
                return None
            value = value[key]
        return value

    def _parse_dataset_fields(self, dataset_fields: str) -> list[str]:
        """Convert a string of comma-separated fields into a list of fields."""
        dataset_fields = dataset_fields.replace('\'', '').replace('"', '').replace('`', '')
        return [field.strip() for field in dataset_fields.split(",")]

    def _flatten(self, d: dict) -> dict:
        """Flatten a nested dictionary."""
        def items():
            for key, value in d.items():
                if isinstance(value, dict):
                    for subkey, subvalue in self._flatten(value).items():
                        yield key + "_" + subkey, subvalue
                else:
                    yield key, value
        return dict(items())
