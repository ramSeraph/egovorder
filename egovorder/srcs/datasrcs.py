# Adapted from egazette/srcs/datasrcs.py for egovorder.
# Modified for the GOIR-only extraction; see LICENSE for licensing scope.

"""The complete set of supported government-order sources."""

from .andhra import AndhraGOIR
from .telangana import TelanganaGOIR
from .uttarakhand import UttarakhandGO
from .datasrcs_info import srcinfos

srcdict = {
    "andhra_goir": AndhraGOIR,
    "telangana_goir": TelanganaGOIR,
    "uttarakhand_gos": UttarakhandGO,
}


def get_srcobjs(srclist, storage):
    """Use all registered sources by default, and reject unsupported names."""
    names = list(dict.fromkeys(srclist or srcdict))
    unknown = set(names) - srcdict.keys()
    if unknown:
        raise ValueError(f"Unknown sources: {', '.join(sorted(unknown))}")
    return [
        srcdict[name](name, storage)
        for name in names
        if srcinfos[name].get("enabled", True)
    ]
