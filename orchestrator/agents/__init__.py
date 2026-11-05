#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agent 注册表：stage → agent 实例。"""

from .archivist import Archivist
from .composer import Composer
from .critic import Critic
from .gatekeeper import Gatekeeper
from .ledger import Ledger
from .researcher import Researcher
from .scout import Scout
from .transcriber import Transcriber
from .writer import Writer

AGENTS = {
    "scout": Scout(),
    "transcriber": Transcriber(),
    "ledger": Ledger(),
    "researcher": Researcher(),
    "writer": Writer(),
    "critic": Critic(),
    "composer": Composer(),
    "gatekeeper": Gatekeeper(),
    "archivist": Archivist(),
}

__all__ = ["AGENTS"]
