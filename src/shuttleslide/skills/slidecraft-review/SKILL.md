---
name: slidecraft-review
description: Launch an interactive web UI for reviewing, editing, and approving AI-generated slide presentations with human-in-the-loop feedback. Use when the user says review the slides, check the generated presentation, see slides before finalizing, or wants a visual review interface for slide generation.
allowed-tools: Bash(slidecraft review *) Bash(slidecraft *)
---

# Interactive slide review web UI

## Quick start
    slidecraft review

Opens browser at http://127.0.0.1:8765. Ctrl+C to stop.

## Options
    slidecraft review --port 9000
    slidecraft review --no-browser
    slidecraft review -o tmp/my_runs/
    slidecraft review --api-base $URL --api-key $KEY --model glm-4.7
    slidecraft review --mock

## What it does
1. Configuration screen: set API credentials, topic, style.
2. Pipeline runs with stage-by-stage approval.
3. Review each snapshot, request edits, approve to proceed.
4. Output saved to timestamped subdirectories.

## Credential sources (later wins)
1. Web form (runtime)
2. .env file using SHUTTLESLIDE_* keys
3. CLI flags
