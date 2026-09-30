"""Kernel infrastructure configuration.

``core`` is where the things the kernel needs *before* it can do any work live: today, the
bootloader (which models exist, which tier each belongs to, and whether they answer). It holds
no workflow state and imports nothing from ``orchestration``.
"""
