from pathlib import Path

from langchain_core.tools import BaseTool

from fp2mp_core.tools.blocksnet.data import make_data_tools
from fp2mp_core.tools.blocksnet.indicators import make_indicators_tools
from fp2mp_core.tools.blocksnet.network import make_network_tools
from fp2mp_core.tools.blocksnet.provision import make_provision_tools
from fp2mp_core.tools.blocksnet.services import make_services_tools


def make_tools(state: dict, data_dir: Path, output_dir: Path) -> list[BaseTool]:
    ctx = {"state": state, "data_dir": data_dir, "output_dir": output_dir}
    return (
        make_data_tools(ctx)
        + make_network_tools(ctx)
        + make_provision_tools(ctx)
        + make_services_tools(ctx)
        + make_indicators_tools(ctx)
    )


__all__ = ["make_tools"]
