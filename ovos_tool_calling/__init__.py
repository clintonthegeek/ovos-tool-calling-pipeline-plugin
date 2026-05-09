from typing import Dict, List, Optional, Union

from ovos_bus_client.client import MessageBusClient
from ovos_bus_client.message import Message
from ovos_plugin_manager.templates.pipeline import (
    ConfidenceMatcherPipeline,
    IntentHandlerMatch,
)
from ovos_utils.fakebus import FakeBus
from ovos_utils.log import LOG


class ToolCallingPipeline(ConfidenceMatcherPipeline):
    """LLM-orchestrator pipeline plugin.

    v0 stub: logs every utterance it sees and returns no match, letting the
    rest of the pipeline run normally. Useful as a sanity check that the
    plugin is loaded and wired into the intent service correctly.
    """

    def __init__(
        self,
        bus: Optional[Union[MessageBusClient, FakeBus]] = None,
        config: Optional[Dict] = None,
    ):
        super().__init__(bus=bus, config=config)
        LOG.info("ToolCallingPipeline loaded (v0 stub) — config=%s", self.config)

    def match_high(
        self, utterances: List[str], lang: str, message: Message
    ) -> Optional[IntentHandlerMatch]:
        LOG.info("[tool-calling] match_high lang=%s utterances=%r", lang, utterances)
        return None

    def match_medium(
        self, utterances: List[str], lang: str, message: Message
    ) -> Optional[IntentHandlerMatch]:
        LOG.info("[tool-calling] match_medium lang=%s utterances=%r", lang, utterances)
        return None

    def match_low(
        self, utterances: List[str], lang: str, message: Message
    ) -> Optional[IntentHandlerMatch]:
        LOG.info("[tool-calling] match_low lang=%s utterances=%r", lang, utterances)
        return None
