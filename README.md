# chehui

`chehui` is an AstrBot plugin that automatically recalls bot messages after a configured delay when incoming messages match specific keyword rules.

## Features

- Matches incoming messages by configurable keyword prefixes.
- Recalls the bot's next sent message in the same private chat or group chat.
- Supports delayed recall per rule.
- Falls back to recent message history when the send action does not return a message ID.
- Cleans up pending recall tasks when the plugin is unloaded.

## Installation

1. Copy this folder to the AstrBot plugin directory:

   ```text
   data/plugins/chehui
   ```

2. Restart AstrBot or reload plugins from the AstrBot dashboard.

## Configuration

Edit `config.py` and update `retraction_rules`.

Each rule contains:

- `keywords`: A set of message prefixes that trigger recall.
- `delay`: The number of seconds to wait before recalling the bot message.

Example:

```python
retraction_rules: list[dict[str, set[str] | int]] = [
    {
        "keywords": {"签到", "打卡"},
        "delay": 60,
    },
]
```

When a user sends a message that starts with one of the configured keywords, the plugin binds the rule to the current event and recalls the bot's next outgoing message in the same conversation after the configured delay.

## Notes

- The plugin depends on AstrBot's plugin runtime and OneBot-compatible message actions.
- Recall success depends on the bot account's permissions and the adapter's support for recall actions.
- The current source files may display garbled Chinese text if opened with the wrong file encoding. Use UTF-8 when editing new documentation and configuration changes.

## License

This project is released under the license in `LICENSE`. You may freely use, copy, modify, distribute, and further develop this project.
