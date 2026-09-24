# SPDX-License-Identifier: EUPL-1.2-only
# Copyright (C) 2026 FIDAA contributors
# SPDX-FileCopyrightText: 2026 FIDAA contributors
#
# Licensed under the EUPL, Version 1.2 only (the "Licence");
# You may not use this work except in compliance with the Licence.
# You may obtain a copy of the Licence at:
#   https://eupl.eu/1.2/en/
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the Licence is distributed on an "AS IS" basis,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""HTML transcript export — the `transcript` chat command (app.py).

Reads the thread's steps + feedback from the shared database (accounts.py
connection) and renders templates/export.html with chevron.
"""

from datetime import datetime

import chevron

from accounts import db

_STEP_STYLES = {
    "user_message": ("👤 User", "#2563eb"),
    "assistant_message": ("🤖 FIADA", "#059669"),
    "llm": ("💭 Thinking", "#7c3aed"),
    "tool": ("🔧 Recherche", "#d97706"),
    "run": ("▶ Run", "#6b7280"),
}


def build_export_html(thread_id: str) -> str:
    query = """
    SELECT s.type, s.name, s.input, s.output, s."createdAt",
           f.value AS fb_value, f.comment AS fb_comment
    FROM "Step" s
    LEFT JOIN "Feedback" f ON f."stepId" = s.id
    WHERE s."threadId" = %s
    ORDER BY s."startTime"
    """
    rows = db.execute_sql(query, (thread_id,)).fetchall()

    steps = []
    for row in rows:
        step_type, step_name, inp, outp, created_at, fb_value, fb_comment = row
        label, color = _STEP_STYLES.get(step_type, (step_type or "?", "#6b7280"))
        has_feedback = fb_value is not None
        positive = has_feedback and fb_value > 0

        steps.append(
            {
                "display_name": step_name or label,
                "color": color,
                "content": outp or inp or "",
                "time": created_at.strftime("%H:%M:%S") if created_at else "",
                "has_feedback": has_feedback,
                "feedback_class": "up" if positive else "down",
                "feedback_icon": "👍" if positive else "👎",
                "feedback_comment": fb_comment or "",
            }
        )

    with open("templates/export.html", encoding="utf-8") as f:
        return chevron.render(
            f,
            {
                "exported_at": datetime.now().strftime("%d.%m.%Y um %H:%M"),
                "step_count": len(rows),
                "thread_id": thread_id,
                "steps": steps,
            },
        )
