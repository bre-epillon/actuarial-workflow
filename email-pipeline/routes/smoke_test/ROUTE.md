---
name: smoke_test
description: >
  ONLY for pipeline test emails whose subject starts with "[PIPELINE TEST]".
  Never use this route for real business emails.
examples: ["[PIPELINE TEST] hello"]
allowed_tools: [Read, Write, Glob]
---
You are testing the pipeline end-to-end.

1. Read ./extracted/summary.json and the email body it points to.
2. Write ./output/smoke_test/echo.md containing the subject, the sender,
   the list of attachments, and a one-sentence description of the body.
3. Write the summary.md as instructed in the task.
