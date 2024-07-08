"""General-purpose data pipeline."""

from typing import MutableMapping, Optional

import numpy as np

from alphafold3_pytorch.common.biomolecule import Biomolecule, _from_mmcif_object
from alphafold3_pytorch.data import mmcif_parsing
from alphafold3_pytorch.utils.pylogger import RankedLogger

FeatureDict = MutableMapping[str, np.ndarray]

log = RankedLogger(__name__, rank_zero_only=True)


def make_sequence_features(sequence: str, description: str, num_res: int) -> FeatureDict:
    """Construct a feature dict of sequence features."""
    features = {}
    features["between_segment_residues"] = np.zeros((num_res,), dtype=np.int32)
    features["domain_name"] = np.array([description.encode("utf-8")], dtype=object)
    features["seq_length"] = np.array([num_res] * num_res, dtype=np.int32)
    features["sequence"] = np.array([sequence.encode("utf-8")], dtype=object)
    return features


def get_assembly(biomol: Biomolecule, assembly_id: Optional[int] = None) -> Biomolecule:
    """
    Get a specified (Biomolecule) assembly of a given Biomolecule.

    Adapted from: https://github.com/biotite-dev/biotite

    :param biomol: The Biomolecule from which to extract the requested assembly.
    :param assembly_id: The index of the assembly to get.
    :return: The requested assembly.
    """
    # Get mmCIF metadata categories
    assembly_category = mmcif_parsing.mmcif_loop_to_dict(
        "_pdbx_struct_assembly.", "_pdbx_struct_assembly.id", biomol.mmcif_metadata
    )
    assembly_gen_category = mmcif_parsing.mmcif_loop_to_dict(
        "_pdbx_struct_assembly_gen.",
        "_pdbx_struct_assembly_gen.assembly_id",
        biomol.mmcif_metadata,
    )
    struct_oper_category = mmcif_parsing.mmcif_loop_to_dict(
        "_pdbx_struct_oper_list.", "_pdbx_struct_oper_list.id", biomol.mmcif_metadata
    )

    if not all((assembly_category, assembly_gen_category, struct_oper_category)):
        log.warning(
            "Not all required assembly information was found in the mmCIF file. Returning the input biomolecule."
        )
        return biomol

    assembly_ids = sorted(list(assembly_gen_category.keys()))
    if assembly_id is None:
        # NOTE: Sorting ensures that the default assembly is the first biological assembly.
        assembly_id = assembly_ids[0]
    elif assembly_id not in assembly_ids:
        raise KeyError(
            f"Biomolecule has no assembly ID `{assembly_id}`. Available assembly IDs: `{assembly_ids}`."
        )

    # Calculate all possible transformations
    transformations = mmcif_parsing.get_transformations(struct_oper_category)

    # Get transformations and apply them to the affected asym IDs
    assembly = None
    for id in assembly_gen_category:
        op_expr = assembly_gen_category[id]["_pdbx_struct_assembly_gen.oper_expression"]
        asym_id_expr = assembly_gen_category[id]["_pdbx_struct_assembly_gen.asym_id_list"]

        # Find the operation expressions for given assembly ID,
        # where we already asserted that the ID is actually present
        if id == assembly_id:
            operations = mmcif_parsing.parse_operation_expression(op_expr)
            asym_ids = asym_id_expr.split(",")
            # Filter affected asym IDs
            sub_assembly = mmcif_parsing.apply_transformations(
                biomol,
                transformations,
                operations,
                asym_ids,
            )
            # Merge the chains with asym IDs for this operation
            # with chains from other operations
            if assembly is None:
                assembly = sub_assembly
            else:
                assembly += sub_assembly

    assert assembly is not None, f"Assembly with ID `{assembly_id}` not found."
    return Biomolecule(
        atom_positions=assembly.atom_positions,
        atom_mask=assembly.atom_mask,
        b_factors=assembly.b_factors,
        chain_index=assembly.chain_index,
        chemid=assembly.chemid,
        chemtype=assembly.chemtype,
        residue_index=assembly.residue_index,
        restype=assembly.restype,
        chem_comp_table=biomol.chem_comp_table,
        # TODO: Ensure `entity_to_chain` and `mmcif_to_author_chain` are repeated for the assembly
        entity_to_chain=biomol.entity_to_chain,
        mmcif_to_author_chain=biomol.mmcif_to_author_chain,
        mmcif_metadata=biomol.mmcif_metadata,
    )


def make_mmcif_features(mmcif_object: mmcif_parsing.MmcifObject) -> FeatureDict:
    """Make features from an mmCIF object."""
    input_sequence = "".join(
        mmcif_object.chain_to_seqres[chain_id] for chain_id in mmcif_object.chain_to_seqres
    )
    description = mmcif_object.file_id
    num_res = len(input_sequence)

    mmcif_feats = {}

    mmcif_feats.update(
        make_sequence_features(
            sequence=input_sequence,
            description=description,
            num_res=num_res,
        )
    )

    # Expand the first bioassembly/model sequence and structure, to obtain a biologically relevant complex (AF3 Supplement, Section 2.1).
    # Reference: https://github.com/biotite-dev/biotite/blob/1045f43f80c77a0dc00865e924442385ce8f83ab/src/biotite/structure/io/pdbx/convert.py#L1441

    assembly = get_assembly(_from_mmcif_object(mmcif_object))

    mmcif_feats["all_atom_positions"] = assembly.atom_positions
    mmcif_feats["all_atom_mask"] = assembly.atom_mask
    mmcif_feats["b_factors"] = assembly.b_factors
    mmcif_feats["chain_index"] = assembly.chain_index
    mmcif_feats["chemid"] = assembly.chemid
    mmcif_feats["chemtype"] = assembly.chemtype
    mmcif_feats["residue_index"] = assembly.residue_index
    mmcif_feats["restype"] = assembly.restype

    mmcif_feats["resolution"] = np.array([mmcif_object.header["resolution"]], dtype=np.float32)

    mmcif_feats["release_date"] = np.array(
        [mmcif_object.header["release_date"].encode("utf-8")], dtype=object
    )

    mmcif_feats["is_distillation"] = np.array(0.0, dtype=np.float32)

    return mmcif_feats


if __name__ == "__main__":
    mmcif_object = mmcif_parsing.parse_mmcif_object(
        # Load an example mmCIF file that includes
        # protein, nucleic acid, and ligand residues.
        filepath="data/pdb_data/mmcifs/16/316d.cif",
        file_id="316d",
    )
    mmcif_feats = make_mmcif_features(mmcif_object)
