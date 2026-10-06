import os

# A push or fetch can start `git gc --auto` in the background. It writes into
# `objects/pack` while a test's temporary repo is being removed, and the cleanup
# then fails with "Directory not empty". Every git the tests run inherits this.
os.environ.update(
    {
        "GIT_CONFIG_COUNT": "3",
        "GIT_CONFIG_KEY_0": "gc.auto",
        "GIT_CONFIG_VALUE_0": "0",
        "GIT_CONFIG_KEY_1": "receive.autogc",
        "GIT_CONFIG_VALUE_1": "false",
        "GIT_CONFIG_KEY_2": "maintenance.auto",
        "GIT_CONFIG_VALUE_2": "false",
    }
)
