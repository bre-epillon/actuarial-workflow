"""`python -m pipeline`: same as the `email-pipeline` command.

Preferred on Windows laptops where Microsoft Defender blocks unsigned,
rarely-seen executables such as the email-pipeline.exe launcher uv generates.
"""
from .main import main

main()
