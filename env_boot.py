"""Environment bootstrap: loads ``.env`` so that ``.env`` wins over the shell.

Every entrypoint (``app.py``, ``main.py``) starts here, before it imports the registry or
anything that reads a key.

The default ``load_dotenv()`` does *not* override a variable that is already exported, so
an exported name silently beats the value in ``.env``. That is not hypothetical: a stale
``OPENAI_API_KEY`` left in the shell kept being sent instead of the one in ``.env``, and
every request came back saying the token was invalid, with nothing to indicate the app had
read a different token than the file the operator edits. Loading with ``override=True``
makes ``.env`` this app's single source of truth, and the names that were replaced are
returned so the launcher can say so out loud.

The path is resolved against this file rather than the process working directory, matching
``tools.file_ops.read_environment_variables`` so the sidebar panel and the running app can
never disagree about which ``.env`` is in force.
"""

import os
from typing import List

# The app's configuration file, beside this module and therefore beside app.py/main.py.
ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def load_environment(path: str = None) -> List[str]:
    """Loads ``.env`` over the environment and returns the names it replaced.

    Returns the names whose exported value differed from the file, in file order. A name
    that was not exported at all, or was exported with the identical value, is not
    reported -- it was not shadowing anything.

    A missing file is an ordinary start (``[]``): a checkout without a ``.env`` is a
    legitimate state. An unreadable one is reported and also treated as ``[]``, because a
    boot step that raised here would stop the app from launching at all, which is a worse
    failure than a configuration that is visibly incomplete.
    """
    from dotenv import dotenv_values, load_dotenv

    target = path or ENV_PATH
    if not os.path.isfile(target):
        return []
    try:
        file_values = dotenv_values(target)
        replaced = [
            name
            for name, value in file_values.items()
            if value is not None and os.environ.get(name) not in (None, value)
        ]
        load_dotenv(target, override=True)
    except Exception as exc:
        print(f"[Config] could not load {os.path.basename(target)}: {exc}")
        return []
    return replaced
