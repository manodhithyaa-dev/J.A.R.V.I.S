
import os
from pathlib import Path

import litellm
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from models import MODEL_REGISTRY


# =====================================================
# INITIALIZATION
# =====================================================

load_dotenv()

console = Console()

BASE_DIR = Path(__file__).resolve().parent
PROMPT_FILE = BASE_DIR / "prompt.md"

if not PROMPT_FILE.exists():
    raise FileNotFoundError("prompt.md was not found!")

SYSTEM_PROMPT = PROMPT_FILE.read_text(encoding="utf-8")

# Default model
DEFAULT_GROUP = "local"
DEFAULT_MODEL = "gemma4:e4b"

current_group = DEFAULT_GROUP
current_model = DEFAULT_MODEL

conversation = [
    {
        "role": "system",
        "content": SYSTEM_PROMPT
    }
]


# =====================================================
# MODEL REGISTRY
# =====================================================

def get_available_models():
    """
    Flatten MODEL_REGISTRY into a list of usable
    text-chat models.
    """

    available = []

    for group, provider_data in MODEL_REGISTRY.items():

        provider = provider_data["provider"]

        for model in provider_data["models"]:

            # Skip non-chat models for this CLI.
            if model.get("type") in ["embedding", "audio", "vision"]:
                continue

            available.append({
                "group": group,
                "provider": provider,
                "id": model["id"],
                "name": model["name"],
                "description": model.get("description", "")
            })

    return available


AVAILABLE_MODELS = get_available_models()


def find_model(group, model_id):
    for model in AVAILABLE_MODELS:
        if (
            model["group"] == group
            and model["id"] == model_id
        ):
            return model

    return None


# =====================================================
# LITELLM CONFIGURATION
# =====================================================

def get_litellm_config(group, model_id):
    """
    Convert registry entries into LiteLLM configuration.
    API keys and endpoints come from .env.
    """

    model_data = find_model(group, model_id)

    if not model_data:
        raise ValueError("Model not found in registry.")

    provider = model_data["provider"]
    raw_model_id = model_data["id"]

    config = {}

    # -----------------------------
    # OLLAMA
    # -----------------------------

    if provider == "ollama":

        config["model"] = f"ollama_chat/{raw_model_id}"

        config["api_base"] = os.getenv(
            "OLLAMA_API_BASE",
            "http://localhost:11434"
        )

    # -----------------------------
    # OPENROUTER
    # -----------------------------

    elif provider == "openrouter":

        if raw_model_id.startswith("openrouter/"):
            config["model"] = raw_model_id
        else:
            config["model"] = f"openrouter/{raw_model_id}"

        config["api_key"] = os.getenv("OPENROUTER_API_KEY")

        config["api_base"] = os.getenv(
            "OPENROUTER_API_BASE",
            "https://openrouter.ai/api/v1"
        )

    # -----------------------------
    # GEMINI
    # -----------------------------

    elif provider == "gemini":

        config["model"] = (
            raw_model_id
            if raw_model_id.startswith("gemini/")
            else f"gemini/{raw_model_id}"
        )

        config["api_key"] = os.getenv("GEMINI_API_KEY")

        api_base = os.getenv("GEMINI_API_BASE")

        if api_base:
            config["api_base"] = api_base

    # -----------------------------
    # OPENAI
    # -----------------------------

    elif provider == "openai":

        config["model"] = (
            raw_model_id
            if raw_model_id.startswith("openai/")
            else f"openai/{raw_model_id}"
        )

        config["api_key"] = os.getenv("OPENAI_API_KEY")

        api_base = os.getenv("OPENAI_API_BASE")

        if api_base:
            config["api_base"] = api_base

    else:
        raise ValueError(f"Unsupported provider: {provider}")

    # Avoid passing empty credentials.
    config = {
        key: value
        for key, value in config.items()
        if value is not None and value != ""
    }

    return config


# =====================================================
# MODEL LIST
# =====================================================

def display_models():

    table = Table(
        title="J.A.R.V.I.S. MODEL REGISTRY",
        show_lines=True
    )

    table.add_column("No.", style="cyan", justify="right")
    table.add_column("Group", style="magenta")
    table.add_column("Model", style="green")
    table.add_column("Provider", style="yellow")

    for index, model in enumerate(AVAILABLE_MODELS, start=1):

        active = (
            model["group"] == current_group
            and model["id"] == current_model
        )

        name = model["name"]

        if active:
            name += " [ACTIVE]"

        table.add_row(
            str(index),
            model["group"],
            name,
            model["provider"]
        )

    console.print(table)


# =====================================================
# MODEL SWITCHING
# =====================================================

def switch_model():

    global current_group, current_model

    display_models()

    selection = Prompt.ask(
        "\nEnter model number",
        default=""
    ).strip()

    if not selection.isdigit():
        console.print("[red]Invalid selection.[/red]")
        return

    index = int(selection) - 1

    if index < 0 or index >= len(AVAILABLE_MODELS):
        console.print("[red]Model does not exist.[/red]")
        return

    selected = AVAILABLE_MODELS[index]

    current_group = selected["group"]
    current_model = selected["id"]

    console.print(
        Panel(
            f"[bold green]Model switched successfully[/bold green]\n\n"
            f"Model: {selected['name']}\n"
            f"Provider: {selected['provider']}\n"
            f"ID: {selected['id']}",
            border_style="green"
        )
    )


# =====================================================
# CHAT
# =====================================================

def chat(user_message):

    messages = conversation + [
        {
            "role": "user",
            "content": user_message
        }
    ]

    try:

        config = get_litellm_config(
            current_group,
            current_model
        )

        console.print("\n[bold cyan]J.A.R.V.I.S.[/bold cyan]")

        response = litellm.completion(
            messages=messages,
            stream=True,
            **config
        )

        assistant_response = ""

        for chunk in response:

            if not chunk.choices:
                continue

            content = chunk.choices[0].delta.content

            if isinstance(content, str):
                console.print(content, end="")
                assistant_response += content

        console.print()

        # Save history only after a successful response.
        if assistant_response:
            conversation.append({
                "role": "user",
                "content": user_message
            })

            conversation.append({
                "role": "assistant",
                "content": assistant_response
            })

    except Exception as error:

        console.print(
            f"\n[bold red]LLM Error:[/bold red] {error}"
        )


# =====================================================
# COMMANDS
# =====================================================

def clear_conversation():

    conversation.clear()

    conversation.append({
        "role": "system",
        "content": SYSTEM_PROMPT
    })

    console.print("[green]Conversation cleared.[/green]")


def show_help():

    console.print(
        Panel(
            """
[bold cyan]/models[/bold cyan]  - List available models

[bold cyan]/model[/bold cyan]   - Switch active model

[bold cyan]/clear[/bold cyan]   - Clear conversation history

[bold cyan]/help[/bold cyan]    - Display commands

[bold cyan]/exit[/bold cyan]    - Exit J.A.R.V.I.S.
            """,
            title="COMMANDS",
            border_style="cyan"
        )
    )


# =====================================================
# MAIN CLI
# =====================================================

def main():

    global current_group, current_model

    if not find_model(current_group, current_model):
        raise ValueError(
            f"Default model {current_group}/{current_model} "
            "does not exist in MODEL_REGISTRY."
        )

    console.print(
        Panel(
            "[bold cyan]J.A.R.V.I.S.[/bold cyan]\n"
            "Personal AI Assistant\n\n"
            f"Provider: {current_group}\n"
            f"Model: {current_model}\n\n"
            "Type /help for commands.",
            title="SYSTEM ONLINE",
            border_style="cyan"
        )
    )

    while True:

        try:

            user_input = Prompt.ask(
                "\n[bold green]You[/bold green]"
            ).strip()

            if not user_input:
                continue

            if user_input == "/exit":

                console.print(
                    "[cyan]J.A.R.V.I.S. shutting down.[/cyan]"
                )
                break

            elif user_input == "/help":

                show_help()

            elif user_input == "/models":

                display_models()

            elif user_input == "/model":

                switch_model()

            elif user_input == "/clear":

                clear_conversation()

            else:

                chat(user_input)

        except KeyboardInterrupt:

            console.print(
                "\n[yellow]Use /exit to shut down.[/yellow]"
            )


if __name__ == "__main__":
    main()
