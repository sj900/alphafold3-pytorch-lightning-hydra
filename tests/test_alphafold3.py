"""This file prepares unit tests for AlphaFold 3 modules."""

import itertools
import os
import random

import pytest
import rootutils
import torch
from einops import repeat

rootutils.setup_root(__file__, indicator=".project-root", pythonpath=True)

from alphafold3_pytorch import (
    Alphafold3,
    Attention,
    CentreRandomAugmentation,
    ComputeAlignmentError,
    ComputeModelSelectionScore,
    ComputeRankingScore,
    ConfidenceHead,
    ConfidenceHeadLogits,
    DiffusionModule,
    DiffusionTransformer,
    DistogramHead,
    ElucidatedAtomDiffusion,
    ExpressCoordinatesInFrame,
    InputFeatureEmbedder,
    MSAModule,
    PairformerStack,
    RelativePositionEncoding,
    RigidFrom3Points,
    SmoothLDDTLoss,
    TemplateEmbedder,
    WeightedRigidAlign,
)
from alphafold3_pytorch.data.atom_datamodule import MockAtomDataset
from alphafold3_pytorch.data.pdb_datamodule import (
    alphafold3_inputs_to_batched_atom_input,
    collate_inputs_to_batched_atom_input,
)
from alphafold3_pytorch.models.components.alphafold3 import (
    batch_repeat_interleave,
    full_pairwise_repr_to_windowed,
    get_cid_molecule_type,
    mean_pool_fixed_windows_with_mask,
    mean_pool_with_lens,
)
from alphafold3_pytorch.models.components.inputs import (
    IS_MOLECULE_TYPES,
    IS_PROTEIN,
    Alphafold3Input,
    PDBInput,
    atom_ref_pos_to_atompair_inputs,
    molecule_to_atom_input,
    pdb_input_to_molecule_input,
)
from alphafold3_pytorch.utils.model_utils import exclusive_cumsum
from alphafold3_pytorch.utils.utils import exists

os.environ["TYPECHECK"] = "True"
os.environ["DEBUG"] = "True"

DATA_TEST_PDB_ID = "721p"


def test_atom_ref_pos_to_atompair_inputs():
    """Test the function to convert atom reference positions to atom pair inputs."""
    atom_ref_pos = torch.randn(16, 3)
    atom_ref_space_uid = torch.ones(16).long()

    atompair_inputs = atom_ref_pos_to_atompair_inputs(atom_ref_pos, atom_ref_space_uid)

    assert atompair_inputs.shape == (16, 16, 5)


def test_mean_pool_with_lens():
    """Test mean pooling with lengths."""
    seq = torch.tensor([[[1.0], [1.0], [1.0], [2.0], [2.0], [2.0], [2.0], [1.0], [1.0]]])
    lens = torch.tensor([[3, 4, 2]]).long()
    pooled = mean_pool_with_lens(seq, lens)

    assert torch.allclose(pooled, torch.tensor([[[1.0], [2.0], [1.0]]]))


def test_mean_pool_with_mask():
    """Test mean pooling with mask."""
    seq = torch.tensor([[[1.0], [100.0], [1.0], [2.0], [2.0], [100.0], [1.0], [1.0], [100.0]]])
    mask = torch.tensor([[True, False, True, True, True, False, True, True, False]])

    pooled, _, inverse_function = mean_pool_fixed_windows_with_mask(
        seq, mask, window_size=3, return_mask_and_inverse=True
    )

    assert inverse_function(pooled).shape == seq.shape
    assert torch.allclose(pooled, torch.tensor([[[1.0], [2.0], [1.0]]]))


def test_batch_repeat_interleave():
    """Test repeating consecutive elements with lengths."""
    seq = torch.tensor([[[1.0], [2.0], [4.0]], [[1.0], [2.0], [4.0]]])
    lens = torch.tensor([[3, 4, 2], [2, 5, 1]]).long()
    repeated = batch_repeat_interleave(seq, lens)
    assert torch.allclose(
        repeated,
        torch.tensor(
            [
                [[1.0], [1.0], [1.0], [2.0], [2.0], [2.0], [2.0], [4.0], [4.0]],
                [[1.0], [1.0], [2.0], [2.0], [2.0], [2.0], [2.0], [4.0], [0.0]],
            ]
        ),
    )


def test_smooth_lddt_loss():
    """Test the smooth lDDT loss function."""
    pred_coords = torch.randn(2, 100, 3)
    true_coords = torch.randn(2, 100, 3)
    is_dna = torch.randint(0, 2, (2, 100)).bool()
    is_rna = torch.randint(0, 2, (2, 100)).bool()

    loss_fn = SmoothLDDTLoss()
    loss = loss_fn(pred_coords, true_coords, is_dna, is_rna)

    assert loss.numel() == 1


def test_weighted_rigid_align():
    """Test the weighted rigid alignment function."""
    pred_coords = torch.randn(2, 100, 3)
    weights = torch.rand(2, 100)

    align_fn = WeightedRigidAlign()
    aligned_coords = align_fn(pred_coords, pred_coords, weights)

    # `pred_coords` should match itself without any change after alignment

    rmsd = torch.sqrt(((pred_coords - aligned_coords) ** 2).sum(dim=-1).mean(dim=-1))
    assert (rmsd < 1e-5).all()

    random_augment_fn = CentreRandomAugmentation()
    aligned_coords = align_fn(pred_coords, random_augment_fn(pred_coords), weights)

    # `pred_coords` should match a random augmentation of itself after alignment

    rmsd = torch.sqrt(((pred_coords - aligned_coords) ** 2).sum(dim=-1).mean(dim=-1))
    assert (rmsd < 1e-5).all()


def test_weighted_rigid_align_with_mask():
    """Test the weighted rigid alignment function with masking."""
    pred_coords = torch.randn(2, 100, 3)
    true_coords = torch.randn(2, 100, 3)
    weights = torch.rand(2, 100)
    mask = torch.randint(0, 2, (2, 100)).bool()

    align_fn = WeightedRigidAlign()

    # with mask

    aligned_coords = align_fn(pred_coords, true_coords, weights, mask=mask)

    # do it one sample at a time without mask

    all_aligned_coords = []

    for one_mask, one_pred_coords, one_true_coords, one_weight in zip(
        mask, pred_coords, true_coords, weights
    ):
        one_aligned_coords = align_fn(
            one_pred_coords[one_mask][None, ...],
            one_true_coords[one_mask][None, ...],
            one_weight[one_mask][None, ...],
        )

        all_aligned_coords.append(one_aligned_coords.squeeze(0))

    aligned_coords_without_mask = torch.cat(all_aligned_coords, dim=0)

    # both ways should come out with about the same results

    assert torch.allclose(aligned_coords[mask], aligned_coords_without_mask, atol=1e-5)


def test_express_coordinates_in_frame():
    """Test the function to express coordinates in a frame."""
    batch_size = 2
    num_coords = 100
    coords = torch.randn(batch_size, num_coords, 3)
    frame = torch.randn(batch_size, num_coords, 3, 3)

    express_fn = ExpressCoordinatesInFrame()
    transformed_coords = express_fn(coords, frame)

    assert transformed_coords.shape == (batch_size, num_coords, 3)

    broadcastable_seq_frame = torch.randn(batch_size, 3, 3)
    transformed_coords = express_fn(coords, broadcastable_seq_frame)

    assert transformed_coords.shape == (batch_size, num_coords, 3)

    broadcastable_batch_and_seq_frame = torch.randn(3, 3)
    transformed_coords = express_fn(coords, broadcastable_batch_and_seq_frame)

    assert transformed_coords.shape == (batch_size, num_coords, 3)


def test_rigid_from_three_points():
    """Test the function to compute a rigid transformation from three points."""
    rigid_from_3_points = RigidFrom3Points()

    points = torch.randn(7, 11, 23, 3)
    rotation, _ = rigid_from_3_points((points, points, points))
    assert rotation.shape == (7, 11, 23, 3, 3)


def test_compute_alignment_error():
    """Test the function to compute alignment error."""
    pred_coords = torch.randn(2, 100, 3)
    pred_frames = torch.randn(2, 100, 3, 3)

    # `pred_coords` should match itself in frame basis

    error_fn = ComputeAlignmentError()
    alignment_errors = error_fn(pred_coords, pred_coords, pred_frames, pred_frames)

    assert alignment_errors.shape == (2, 100, 100)
    assert (alignment_errors.mean(-1) < 1e-3).all()


def test_centre_random_augmentation():
    """Test the function to centre random augmentation."""
    coords = torch.randn(2, 100, 3)

    augmentation_fn = CentreRandomAugmentation()
    augmented_coords = augmentation_fn(coords)

    assert augmented_coords.shape == coords.shape


@pytest.mark.parametrize("checkpoint", (True, False))
@pytest.mark.parametrize("recurrent_depth", (1, 2))
@pytest.mark.parametrize("enable_attn_softclamp", (True, False))
def test_pairformer(checkpoint, recurrent_depth, enable_attn_softclamp):
    """Test the Pairformer stack."""
    single = torch.randn(2, 16, 384).requires_grad_()
    pairwise = torch.randn(2, 16, 16, 128).requires_grad_()
    mask = torch.randint(0, 2, (2, 16)).bool()

    pairformer = PairformerStack(
        depth=4,
        num_register_tokens=4,
        recurrent_depth=recurrent_depth,
        checkpoint=checkpoint,
        pair_bias_attn_kwargs=dict(enable_attn_softclamp=enable_attn_softclamp),
    )

    single_out, pairwise_out = pairformer(single_repr=single, pairwise_repr=pairwise, mask=mask)

    assert single.shape == single_out.shape
    assert pairwise.shape == pairwise_out.shape

    if checkpoint:
        loss = single_out.sum() + pairwise_out.sum()
        loss.backward()


@pytest.mark.parametrize("checkpoint", (False, True))
def test_msa_module(checkpoint):
    """Test the MSA module."""
    single = torch.randn(2, 16, 384).requires_grad_()
    pairwise = torch.randn(2, 16, 16, 128).requires_grad_()
    msa = torch.randn(2, 7, 16, 32)
    mask = torch.randint(0, 2, (2, 16)).bool()
    msa_mask = torch.randint(0, 2, (2, 7)).bool()
    additional_msa_feats = torch.randn(2, 7, 16, 2)

    msa_module = MSAModule(
        checkpoint=checkpoint,
        max_num_msa=3,  # will randomly select 3 out of the MSAs, accounting for mask, using sample without replacement
    )

    pairwise_out = msa_module(
        msa=msa,
        single_repr=single,
        pairwise_repr=pairwise,
        mask=mask,
        msa_mask=msa_mask,
        additional_msa_feats=additional_msa_feats,
    )

    assert pairwise.shape == pairwise_out.shape

    if checkpoint:
        loss = pairwise_out.sum()
        loss.backward()


@pytest.mark.parametrize("serial,checkpoint", ((False, False), (True, False), (True, True)))
@pytest.mark.parametrize("use_linear_attn", (False, True))
@pytest.mark.parametrize("use_colt5_attn", (False, True))
def test_diffusion_transformer(checkpoint, serial, use_linear_attn, use_colt5_attn):
    """Test the diffusion transformer."""
    single = torch.randn(2, 16, 384).requires_grad_()
    pairwise = torch.randn(2, 16, 16, 128).requires_grad_()
    mask = torch.randint(0, 2, (2, 16)).bool()

    diffusion_transformer = DiffusionTransformer(
        depth=2,
        heads=16,
        serial=serial,
        checkpoint=checkpoint,
        use_linear_attn=use_linear_attn,
        use_colt5_attn=use_colt5_attn,
    )

    single_out = diffusion_transformer(
        single, single_repr=single, pairwise_repr=pairwise, mask=mask
    )

    assert single.shape == single_out.shape

    if checkpoint:
        loss = single_out.sum()
        loss.backward()


def test_sequence_local_attn():
    """Test the sequence local attention module."""
    atoms = torch.randn(2, 17, 32)
    attn_bias = torch.randn(2, 17, 17)

    attn = Attention(dim=32, dim_head=16, heads=8, window_size=5)

    out = attn(atoms, attn_bias=attn_bias)
    assert out.shape == atoms.shape


@pytest.mark.parametrize("karras_formulation", (True, False))
def test_diffusion_module(karras_formulation):
    """Test the diffusion module."""
    seq_len = 16
    atom_seq_len = 32

    molecule_atom_lens = torch.full((2, seq_len), 2).long()

    noised_atom_pos = torch.randn(2, atom_seq_len, 3)
    atom_feats = torch.randn(2, atom_seq_len, 128)
    atompair_feats = torch.randn(2, atom_seq_len, atom_seq_len, 16)
    atom_mask = torch.ones((2, atom_seq_len)).bool()

    times = torch.randn(
        2,
    )
    mask = torch.ones(2, seq_len).bool()
    single_trunk_repr = torch.randn(2, seq_len, 128)
    single_inputs_repr = torch.randn(2, seq_len, 256)

    pairwise_trunk = torch.randn(2, seq_len, seq_len, 128)
    pairwise_rel_pos_feats = torch.randn(2, seq_len, seq_len, 12)

    diffusion_module = DiffusionModule(
        atoms_per_window=27,
        dim_pairwise_trunk=128,
        dim_pairwise_rel_pos_feats=12,
        atom_encoder_depth=1,
        atom_decoder_depth=1,
        token_transformer_depth=1,
        atom_encoder_kwargs=dict(attn_num_memory_kv=2),
        token_transformer_kwargs=dict(num_register_tokens=2),
    )

    atom_pos_update = diffusion_module(
        noised_atom_pos,
        times=times,
        atom_feats=atom_feats,
        atompair_feats=atompair_feats,
        atom_mask=atom_mask,
        mask=mask,
        single_trunk_repr=single_trunk_repr,
        single_inputs_repr=single_inputs_repr,
        pairwise_trunk=pairwise_trunk,
        pairwise_rel_pos_feats=pairwise_rel_pos_feats,
        molecule_atom_lens=molecule_atom_lens,
    )

    assert noised_atom_pos.shape == atom_pos_update.shape

    edm = ElucidatedAtomDiffusion(
        diffusion_module, karras_formulation=karras_formulation, num_sample_steps=2
    )

    edm_return = edm(
        noised_atom_pos,
        atom_feats=atom_feats,
        atompair_feats=atompair_feats,
        atom_mask=atom_mask,
        mask=mask,
        single_trunk_repr=single_trunk_repr,
        single_inputs_repr=single_inputs_repr,
        pairwise_trunk=pairwise_trunk,
        pairwise_rel_pos_feats=pairwise_rel_pos_feats,
        molecule_atom_lens=molecule_atom_lens,
        add_bond_loss=True,
    )

    assert edm_return.loss.numel() == 1

    sampled_atom_pos = edm.sample(
        atom_mask=atom_mask,
        atom_feats=atom_feats,
        atompair_feats=atompair_feats,
        mask=mask,
        single_trunk_repr=single_trunk_repr,
        single_inputs_repr=single_inputs_repr,
        pairwise_trunk=pairwise_trunk,
        pairwise_rel_pos_feats=pairwise_rel_pos_feats,
        molecule_atom_lens=molecule_atom_lens,
    )

    assert sampled_atom_pos.shape == noised_atom_pos.shape


def test_relative_position_encoding():
    """Test the relative position encoding module."""
    additional_molecule_feats = torch.randint(0, 2, (8, 100, 5))

    embedder = RelativePositionEncoding()

    rpe_embed = embedder(additional_molecule_feats=additional_molecule_feats)
    assert exists(rpe_embed)


@pytest.mark.parametrize("checkpoint", (False, True))
def test_template_embed(checkpoint):
    """Test the template embedder."""
    template_feats = torch.randn(2, 2, 16, 16, 77)
    template_mask = torch.ones((2, 2)).bool()

    pairwise_repr = torch.randn(2, 16, 16, 128).requires_grad_()
    mask = torch.ones((2, 16)).bool()

    embedder = TemplateEmbedder(dim_template_feats=77, checkpoint=checkpoint)

    template_embed = embedder(
        templates=template_feats,
        template_mask=template_mask,
        pairwise_repr=pairwise_repr,
        mask=mask,
    )

    assert exists(template_embed)

    if checkpoint:
        loss = template_embed.sum()
        loss.backward()


def test_confidence_head():
    """Test the confidence head."""
    seq_len = 16
    atom_seq_len = 32

    molecule_atom_indices = torch.randint(0, 2, (2, seq_len)).long()
    molecule_atom_lens = torch.full((2, seq_len), 2).long()

    atom_offsets = exclusive_cumsum(molecule_atom_lens)

    single_inputs_repr = torch.randn(2, seq_len, 77)
    single_repr = torch.randn(2, seq_len, 384)
    pairwise_repr = torch.randn(2, seq_len, seq_len, 128)
    mask = torch.ones((2, seq_len)).bool()

    atom_feats = torch.randn(2, atom_seq_len, 64)
    pred_atom_pos = torch.randn(2, atom_seq_len, 3)

    # offset indices correctly

    molecule_atom_indices += atom_offsets

    # confidence head

    confidence_head = ConfidenceHead(
        dim_single_inputs=77,
        dim_atom=64,
        atompair_dist_bins=torch.linspace(3, 20, 37).tolist(),
        dim_single=384,
        dim_pairwise=128,
    )

    logits = confidence_head(
        single_inputs_repr=single_inputs_repr,
        single_repr=single_repr,
        pairwise_repr=pairwise_repr,
        pred_atom_pos=pred_atom_pos,
        atom_feats=atom_feats,
        molecule_atom_indices=molecule_atom_indices,
        molecule_atom_lens=molecule_atom_lens,
        mask=mask,
    )

    assert logits.pae.shape[-1] == seq_len
    assert logits.pde.shape[-1] == seq_len

    assert logits.plddt.shape[-1] == atom_seq_len
    assert logits.resolved.shape[-1] == atom_seq_len


def test_input_embedder():
    """Test the input feature embedder."""
    molecule_atom_lens = torch.randint(0, 3, (2, 16))
    atom_seq_len = molecule_atom_lens.sum(dim=-1).amax()
    atom_inputs = torch.randn(2, atom_seq_len, 77)
    atompair_inputs = torch.randn(2, atom_seq_len, atom_seq_len, 5)
    atom_mask = torch.ones((2, atom_seq_len)).bool()
    additional_token_feats = torch.randn(2, 16, 33)
    molecule_ids = torch.randint(0, 32, (2, 16))

    embedder = InputFeatureEmbedder(
        dim_atom_inputs=77,
    )

    embedder(
        atom_inputs=atom_inputs,
        atom_mask=atom_mask,
        atompair_inputs=atompair_inputs,
        molecule_atom_lens=molecule_atom_lens,
        molecule_ids=molecule_ids,
        additional_token_feats=additional_token_feats,
    )


def test_distogram_head():
    """Test the distogram head."""
    pairwise_repr = torch.randn(2, 16, 16, 128)

    distogram_head = DistogramHead(dim_pairwise=128)

    logits = distogram_head(pairwise_repr)

    assert exists(logits)


@pytest.mark.parametrize("window_atompair_inputs", (True, False))
@pytest.mark.parametrize("stochastic_frame_average", (True, False))
@pytest.mark.parametrize("missing_atoms", (True, False))
@pytest.mark.parametrize("calculate_pae", (True, False))
@pytest.mark.parametrize("atom_transformer_intramolecular_attn", (True, False))
@pytest.mark.parametrize("num_molecule_mods", (0, 4))
@pytest.mark.parametrize("distogram_atom_resolution", (True, False))
def test_alphafold3(
    window_atompair_inputs: bool,
    stochastic_frame_average: bool,
    missing_atoms: bool,
    calculate_pae: bool,
    atom_transformer_intramolecular_attn: bool,
    num_molecule_mods: int,
    distogram_atom_resolution: bool,
):
    """Test the AlphaFold 3 model."""
    seq_len = 16
    atoms_per_window = 27

    molecule_atom_indices = torch.randint(0, 2, (2, seq_len)).long()
    molecule_atom_lens = torch.full((2, seq_len), 3).long()

    atom_seq_len = molecule_atom_lens.sum(dim=-1).amax()
    atom_offsets = exclusive_cumsum(molecule_atom_lens)

    token_bonds = torch.randint(0, 2, (2, seq_len, seq_len)).bool()

    atom_inputs = torch.randn(2, atom_seq_len, 77)

    atompair_inputs = torch.randn(2, atom_seq_len, atom_seq_len, 5)
    if window_atompair_inputs:
        atompair_inputs = full_pairwise_repr_to_windowed(
            atompair_inputs, window_size=atoms_per_window
        )

    additional_molecule_feats = torch.randint(0, 2, (2, seq_len, 5))
    additional_token_feats = torch.randn(2, 16, 33)
    is_molecule_types = torch.randint(0, 2, (2, seq_len, IS_MOLECULE_TYPES)).bool()
    molecule_ids = torch.randint(0, 32, (2, seq_len))

    is_molecule_mod = None
    if num_molecule_mods > 0:
        is_molecule_mod = torch.zeros(2, seq_len, num_molecule_mods).uniform_(0, 1) < 0.1

    atom_indices_for_frame = None
    if calculate_pae:
        atom_indices_for_frame = repeat(torch.arange(3), "c -> b n c", b=2, n=seq_len).clone()
        atom_indices_for_frame += atom_offsets[..., None]

    missing_atom_mask = None
    if missing_atoms:
        missing_atom_mask = torch.randint(0, 2, (2, atom_seq_len)).bool()

    atom_parent_ids = None

    if atom_transformer_intramolecular_attn:
        atom_parent_ids = torch.ones(2, atom_seq_len).long()

    template_feats = torch.randn(2, 2, seq_len, seq_len, 44)
    template_mask = torch.ones((2, 2)).bool()

    msa = torch.randn(2, 7, seq_len, 32)
    msa_mask = torch.ones((2, 7)).bool()

    additional_msa_feats = torch.randn(2, 7, seq_len, 2)

    atom_pos = torch.randn(2, atom_seq_len, 3)
    distogram_atom_indices = molecule_atom_lens - 1

    resolved_labels = torch.randint(0, 2, (2, atom_seq_len))

    # offset indices correctly

    distogram_atom_indices += atom_offsets
    molecule_atom_indices += atom_offsets

    # alphafold3

    alphafold3 = Alphafold3(
        dim_atom_inputs=77,
        dim_pairwise=8,
        dim_single=8,
        dim_token=8,
        atoms_per_window=atoms_per_window,
        dim_template_feats=44,
        num_dist_bins=38,
        num_molecule_mods=num_molecule_mods,
        confidence_head_kwargs=dict(pairformer_depth=1),
        template_embedder_kwargs=dict(pairformer_stack_depth=1),
        msa_module_kwargs=dict(
            depth=1,
            dim_msa=8,
        ),
        pairformer_stack=dict(
            depth=1,
            pair_bias_attn_dim_head=4,
            pair_bias_attn_heads=2,
        ),
        diffusion_module_kwargs=dict(
            atom_encoder_depth=1,
            token_transformer_depth=1,
            atom_decoder_depth=1,
            atom_decoder_kwargs=dict(attn_pair_bias_kwargs=dict(dim_head=4)),
            atom_encoder_kwargs=dict(attn_pair_bias_kwargs=dict(dim_head=4)),
        ),
        stochastic_frame_average=stochastic_frame_average,
        distogram_atom_resolution=distogram_atom_resolution,
    )

    loss, breakdown = alphafold3(
        num_recycling_steps=2,
        atom_inputs=atom_inputs,
        molecule_ids=molecule_ids,
        molecule_atom_lens=molecule_atom_lens,
        atom_parent_ids=atom_parent_ids,
        atompair_inputs=atompair_inputs,
        missing_atom_mask=missing_atom_mask,
        atom_indices_for_frame=atom_indices_for_frame,
        is_molecule_types=is_molecule_types,
        is_molecule_mod=is_molecule_mod,
        additional_molecule_feats=additional_molecule_feats,
        additional_msa_feats=additional_msa_feats,
        additional_token_feats=additional_token_feats,
        token_bonds=token_bonds,
        msa=msa,
        msa_mask=msa_mask,
        templates=template_feats,
        template_mask=template_mask,
        atom_pos=atom_pos,
        distogram_atom_indices=distogram_atom_indices,
        molecule_atom_indices=molecule_atom_indices,
        resolved_labels=resolved_labels,
        num_rollout_steps=1,
        diffusion_add_smooth_lddt_loss=True,
        return_loss_breakdown=True,
    )

    loss.backward()

    sampled_atom_pos = alphafold3(
        num_sample_steps=16,
        atom_inputs=atom_inputs,
        molecule_ids=molecule_ids,
        molecule_atom_lens=molecule_atom_lens,
        atompair_inputs=atompair_inputs,
        is_molecule_types=is_molecule_types,
        is_molecule_mod=is_molecule_mod,
        additional_molecule_feats=additional_molecule_feats,
        additional_msa_feats=additional_msa_feats,
        additional_token_feats=additional_token_feats,
        msa=msa,
        templates=template_feats,
        template_mask=template_mask,
    )

    assert sampled_atom_pos.ndim == 3


def test_alphafold3_without_msa_and_templates():
    """Test the AlphaFold 3 model without MSA and templates."""
    seq_len = 16
    atom_seq_len = 32

    molecule_atom_indices = torch.randint(0, 2, (2, seq_len)).long()
    molecule_atom_lens = torch.full((2, seq_len), 2).long()

    atom_offsets = exclusive_cumsum(molecule_atom_lens)

    atom_inputs = torch.randn(2, atom_seq_len, 77)
    atompair_inputs = torch.randn(2, atom_seq_len, atom_seq_len, 5)
    additional_molecule_feats = torch.randint(0, 2, (2, seq_len, 5))
    additional_token_feats = torch.randn(2, seq_len, 33)
    is_molecule_types = torch.randint(0, 2, (2, seq_len, IS_MOLECULE_TYPES)).bool()
    molecule_ids = torch.randint(0, 32, (2, seq_len))

    atom_pos = torch.randn(2, atom_seq_len, 3)
    distogram_atom_indices = molecule_atom_lens - 1

    resolved_labels = torch.randint(0, 2, (2, atom_seq_len))

    # offset indices correctly

    distogram_atom_indices += atom_offsets
    molecule_atom_indices += atom_offsets

    # alphafold3

    alphafold3 = Alphafold3(
        dim_atom_inputs=77,
        dim_template_feats=44,
        num_dist_bins=38,
        num_molecule_mods=0,
        checkpoint_trunk_pairformer=True,
        checkpoint_diffusion_token_transformer=True,
        confidence_head_kwargs=dict(pairformer_depth=1),
        template_embedder_kwargs=dict(pairformer_stack_depth=1),
        msa_module_kwargs=dict(depth=1),
        pairformer_stack=dict(checkpoint=True, depth=2),
        diffusion_module_kwargs=dict(
            atom_encoder_depth=2,
            atom_encoder_kwargs=dict(
                checkpoint=True,
            ),
            token_transformer_depth=2,
            token_transformer_kwargs=dict(
                checkpoint=True,
            ),
            atom_decoder_depth=2,
            atom_decoder_kwargs=dict(
                checkpoint=True,
            ),
        ),
    )

    loss, breakdown = alphafold3(
        num_recycling_steps=2,
        atom_inputs=atom_inputs,
        molecule_ids=molecule_ids,
        molecule_atom_lens=molecule_atom_lens,
        atompair_inputs=atompair_inputs,
        is_molecule_types=is_molecule_types,
        additional_molecule_feats=additional_molecule_feats,
        additional_token_feats=additional_token_feats,
        atom_pos=atom_pos,
        distogram_atom_indices=distogram_atom_indices,
        molecule_atom_indices=molecule_atom_indices,
        resolved_labels=resolved_labels,
        return_loss_breakdown=True,
    )

    loss.backward()


def test_alphafold3_force_return_loss():
    """Test the AlphaFold 3 model with forced loss returning."""
    seq_len = 16
    atom_seq_len = 32

    molecule_atom_indices = torch.randint(0, 2, (2, seq_len)).long()
    molecule_atom_lens = torch.full((2, seq_len), 2).long()

    atom_offsets = exclusive_cumsum(molecule_atom_lens)

    atom_inputs = torch.randn(2, atom_seq_len, 77)
    atompair_inputs = torch.randn(2, atom_seq_len, atom_seq_len, 5)
    additional_molecule_feats = torch.randint(0, 2, (2, seq_len, 5))
    additional_token_feats = torch.randn(2, seq_len, 33)
    is_molecule_types = torch.randint(0, 2, (2, seq_len, IS_MOLECULE_TYPES)).bool()
    molecule_ids = torch.randint(0, 32, (2, seq_len))

    atom_pos = torch.randn(2, atom_seq_len, 3)
    distogram_atom_indices = molecule_atom_lens - 1

    resolved_labels = torch.randint(0, 2, (2, atom_seq_len))

    # offset indices correctly

    distogram_atom_indices += atom_offsets
    molecule_atom_indices += atom_offsets

    # alphafold3

    alphafold3 = Alphafold3(
        dim_atom_inputs=77,
        dim_template_feats=44,
        num_dist_bins=38,
        num_molecule_mods=0,
        confidence_head_kwargs=dict(pairformer_depth=1),
        template_embedder_kwargs=dict(pairformer_stack_depth=1),
        msa_module_kwargs=dict(depth=1),
        pairformer_stack=dict(depth=2),
        diffusion_module_kwargs=dict(
            atom_encoder_depth=1,
            token_transformer_depth=1,
            atom_decoder_depth=1,
        ),
    )

    sampled_atom_pos = alphafold3(
        num_recycling_steps=2,
        atom_inputs=atom_inputs,
        molecule_ids=molecule_ids,
        molecule_atom_lens=molecule_atom_lens,
        atompair_inputs=atompair_inputs,
        is_molecule_types=is_molecule_types,
        additional_molecule_feats=additional_molecule_feats,
        additional_token_feats=additional_token_feats,
        atom_pos=atom_pos,
        distogram_atom_indices=distogram_atom_indices,
        molecule_atom_indices=molecule_atom_indices,
        resolved_labels=resolved_labels,
        return_loss_breakdown=True,
        return_loss=False,  # force sampling even if labels are given
    )

    assert sampled_atom_pos.ndim == 3

    loss, _ = alphafold3(
        num_recycling_steps=2,
        atom_inputs=atom_inputs,
        molecule_ids=molecule_ids,
        molecule_atom_lens=molecule_atom_lens,
        atompair_inputs=atompair_inputs,
        is_molecule_types=is_molecule_types,
        additional_molecule_feats=additional_molecule_feats,
        additional_token_feats=additional_token_feats,
        molecule_atom_indices=molecule_atom_indices,
        return_loss_breakdown=True,
        return_loss=True,  # force returning loss even if no labels given
    )

    assert loss == 0.0


def test_alphafold3_force_return_loss_with_confidence_logits():
    """Test the AlphaFold 3 model with forced returning of losses and confidence logits."""
    seq_len = 16
    atom_seq_len = 32

    molecule_atom_indices = torch.randint(0, 2, (2, seq_len)).long()
    molecule_atom_lens = torch.full((2, seq_len), 2).long()

    atom_offsets = exclusive_cumsum(molecule_atom_lens)

    atom_inputs = torch.randn(2, atom_seq_len, 77)
    atompair_inputs = torch.randn(2, atom_seq_len, atom_seq_len, 5)
    additional_molecule_feats = torch.randint(0, 2, (2, seq_len, 5))
    additional_token_feats = torch.randn(2, seq_len, 33)
    is_molecule_types = torch.randint(0, 2, (2, seq_len, IS_MOLECULE_TYPES)).bool()
    molecule_ids = torch.randint(0, 32, (2, seq_len))

    atom_pos = torch.randn(2, atom_seq_len, 3)
    distogram_atom_indices = molecule_atom_lens - 1

    resolved_labels = torch.randint(0, 2, (2, atom_seq_len))

    # offset indices correctly

    distogram_atom_indices += atom_offsets
    molecule_atom_indices += atom_offsets

    # alphafold3

    alphafold3 = Alphafold3(
        dim_atom_inputs=77,
        dim_template_feats=44,
        num_dist_bins=38,
        num_molecule_mods=0,
        confidence_head_kwargs=dict(pairformer_depth=1),
        template_embedder_kwargs=dict(pairformer_stack_depth=1),
        msa_module_kwargs=dict(depth=1),
        pairformer_stack=dict(depth=2),
        diffusion_module_kwargs=dict(
            atom_encoder_depth=1,
            token_transformer_depth=1,
            atom_decoder_depth=1,
        ),
    )

    sampled_atom_pos, confidence_head_logits = alphafold3(
        num_recycling_steps=2,
        atom_inputs=atom_inputs,
        molecule_ids=molecule_ids,
        molecule_atom_lens=molecule_atom_lens,
        atompair_inputs=atompair_inputs,
        is_molecule_types=is_molecule_types,
        additional_molecule_feats=additional_molecule_feats,
        additional_token_feats=additional_token_feats,
        atom_pos=atom_pos,
        distogram_atom_indices=distogram_atom_indices,
        molecule_atom_indices=molecule_atom_indices,
        resolved_labels=resolved_labels,
        return_loss_breakdown=True,
        return_loss=False,  # force sampling even if labels are given
        return_confidence_head_logits=True,
    )

    assert sampled_atom_pos.ndim == 3


def test_alphafold3_with_atom_and_bond_embeddings():
    """Test the AlphaFold 3 model with atom and bond embeddings."""
    alphafold3 = Alphafold3(
        num_atom_embeds=7,
        num_atompair_embeds=3,
        num_molecule_mods=0,
        dim_atom_inputs=77,
        dim_template_feats=44,
    )

    # mock inputs

    seq_len = 16
    atom_seq_len = 32

    molecule_atom_indices = torch.randint(0, 2, (2, seq_len)).long()
    molecule_atom_lens = torch.full((2, seq_len), 2).long()

    atom_offsets = exclusive_cumsum(molecule_atom_lens)

    atom_ids = torch.randint(0, 7, (2, atom_seq_len))
    atompair_ids = torch.randint(0, 3, (2, atom_seq_len, atom_seq_len))

    atom_inputs = torch.randn(2, atom_seq_len, 77)
    atompair_inputs = torch.randn(2, atom_seq_len, atom_seq_len, 5)

    additional_molecule_feats = torch.randint(0, 2, (2, seq_len, 5))
    additional_token_feats = torch.randn(2, seq_len, 33)
    is_molecule_types = torch.randint(0, 2, (2, seq_len, IS_MOLECULE_TYPES)).bool()
    molecule_ids = torch.randint(0, 32, (2, seq_len))

    template_feats = torch.randn(2, 2, seq_len, seq_len, 44)
    template_mask = torch.ones((2, 2)).bool()

    msa = torch.randn(2, 7, seq_len, 32)
    msa_mask = torch.ones((2, 7)).bool()

    additional_msa_feats = torch.randn(2, 7, seq_len, 2)

    # required for training, but omitted on inference

    atom_pos = torch.randn(2, atom_seq_len, 3)
    distogram_atom_indices = molecule_atom_lens - 1  # last atom, as an example

    resolved_labels = torch.randint(0, 2, (2, atom_seq_len))

    # offset indices correctly

    distogram_atom_indices += atom_offsets
    molecule_atom_indices += atom_offsets

    # alphafold3

    loss = alphafold3(
        num_recycling_steps=2,
        atom_ids=atom_ids,
        atompair_ids=atompair_ids,
        atom_inputs=atom_inputs,
        atompair_inputs=atompair_inputs,
        molecule_ids=molecule_ids,
        molecule_atom_lens=molecule_atom_lens,
        is_molecule_types=is_molecule_types,
        additional_molecule_feats=additional_molecule_feats,
        additional_msa_feats=additional_msa_feats,
        additional_token_feats=additional_token_feats,
        msa=msa,
        msa_mask=msa_mask,
        templates=template_feats,
        template_mask=template_mask,
        atom_pos=atom_pos,
        distogram_atom_indices=distogram_atom_indices,
        molecule_atom_indices=molecule_atom_indices,
        resolved_labels=resolved_labels,
    )

    assert loss.numel() == 1


# test use of collation fn outside of trainer


def test_collate_fn():
    alphafold3 = Alphafold3(
        dim_atom_inputs=77,
        dim_template_feats=44,
        num_dist_bins=38,
        confidence_head_kwargs=dict(pairformer_depth=1),
        template_embedder_kwargs=dict(pairformer_stack_depth=1),
        msa_module_kwargs=dict(depth=1),
        pairformer_stack=dict(depth=1),
        diffusion_module_kwargs=dict(
            atom_encoder_depth=1,
            token_transformer_depth=1,
            atom_decoder_depth=1,
        ),
    )

    dataset = MockAtomDataset(5)

    batched_atom_inputs = collate_inputs_to_batched_atom_input([dataset[i] for i in range(3)])

    _, breakdown = alphafold3(
        **batched_atom_inputs.model_forward_dict(), return_loss_breakdown=True
    )


# test compute ranking score


def test_compute_ranking_score():
    """Test the compute ranking score function."""
    # mock inputs

    batch_size = 2
    seq_len = 16
    atom_seq_len = 32

    molecule_atom_lens = torch.full((2, seq_len), 2).long()

    is_molecule_types = torch.randint(0, 2, (batch_size, seq_len, 5)).bool()
    atom_pos = torch.randn(batch_size, atom_seq_len, 3) * 5
    atom_mask = torch.randint(0, 2, (atom_pos.shape[:-1])).type_as(atom_pos).bool()
    has_frame = torch.randint(0, 2, (batch_size, seq_len)).bool()
    is_modified_residue = torch.randint(0, 2, (batch_size, atom_seq_len))

    pae_logits = torch.randn(batch_size, 64, seq_len, seq_len)
    pde_logits = torch.randn(batch_size, 64, seq_len, seq_len)
    plddt_logits = torch.randn(batch_size, 50, atom_seq_len)
    resolved_logits = torch.randint(0, 2, (batch_size, 2, seq_len))

    confidence_head_logits = ConfidenceHeadLogits(
        pae_logits, pde_logits, plddt_logits, resolved_logits
    )

    chain_length = [random.randint(seq_len // 4, seq_len // 2) for _ in range(batch_size)]  # nosec

    asym_id = torch.tensor(
        [
            [
                item
                for val, count in enumerate([chain_len, seq_len - chain_len])
                for item in itertools.repeat(val, count)
            ]
            for chain_len in chain_length
        ]
    ).long()

    compute_ranking_score = ComputeRankingScore()

    full_complex_metric = compute_ranking_score.compute_full_complex_metric(
        confidence_head_logits,
        asym_id,
        has_frame,
        molecule_atom_lens,
        atom_pos,
        atom_mask,
        is_molecule_types,
    )

    single_chain_metric = compute_ranking_score.compute_single_chain_metric(
        confidence_head_logits,
        asym_id,
        has_frame,
    )

    interface_metric = compute_ranking_score.compute_interface_metric(
        confidence_head_logits, asym_id, has_frame, interface_chains=[(0, 1), (1,)]
    )

    modified_residue_score = compute_ranking_score.compute_modified_residue_score(
        confidence_head_logits, atom_mask, is_modified_residue
    )

    residue_level_ptm_score = compute_ranking_score.compute_confidence_score.compute_ptm(
        pae_logits, asym_id, has_frame
    )

    assert (
        full_complex_metric.numel() == batch_size
    ), f"Full complex metric has wrong shape: {full_complex_metric.shape}"
    assert (
        single_chain_metric.numel() == batch_size
    ), f"Single chain metric has wrong shape: {single_chain_metric.shape}"
    assert (
        interface_metric.numel() == batch_size
    ), f"Interface metric has wrong shape: {interface_metric.shape}"
    assert (
        modified_residue_score.numel() == batch_size
    ), f"Modified residue score has wrong shape: {modified_residue_score.shape}"
    assert (
        residue_level_ptm_score.numel() == batch_size
    ), f"Residue level pTM score has wrong shape: {residue_level_ptm_score.shape}"


def test_model_selection_score():
    """Test the model selection score function."""
    # mock inputs

    batch_size = 2
    seq_len = 16
    atom_seq_len = 32

    molecule_atom_lens = torch.full((2, seq_len), 2).long()

    atom_pos_true = torch.randn(batch_size, atom_seq_len, 3) * 5
    atom_pos_pred = torch.randn(batch_size, atom_seq_len, 3) * 5
    atom_mask = torch.randint(0, 2, (atom_pos_true.shape[:-1])).type_as(atom_pos_true).bool()
    tok_repr_atm_mask = torch.randint(0, 2, (batch_size, seq_len)).bool()

    dist_logits = torch.randn(batch_size, 38, seq_len, seq_len)
    pde_logits = torch.randn(batch_size, 64, seq_len, seq_len)

    chain_length = [random.randint(seq_len // 4, seq_len // 2) for _ in range(batch_size)]  # nosec

    asym_id = torch.tensor(
        [
            [
                item
                for val, count in enumerate([chain_len, seq_len - chain_len])
                for item in itertools.repeat(val, count)
            ]
            for chain_len in chain_length
        ]
    ).long()

    is_molecule_types = torch.zeros_like(asym_id)
    is_molecule_types = torch.nn.functional.one_hot(is_molecule_types, 5).bool()

    compute_model_selection_score = ComputeModelSelectionScore()

    gpde = compute_model_selection_score.compute_gpde(
        pde_logits,
        dist_logits,
        compute_model_selection_score.dist_breaks,
        tok_repr_atm_mask,
    )

    weighted_lddt = compute_model_selection_score.compute_weighted_lddt(
        atom_pos_pred,
        atom_pos_true,
        atom_mask,
        asym_id,
        is_molecule_types,
        molecule_atom_lens,
        chains_list=[(0, 1), (1,)],
        is_fine_tuning=False,
    )

    assert exists(gpde) and exists(weighted_lddt)


def test_model_selection_score_end_to_end():
    """Test the model selection score function end-to-end."""

    # prepare two atom inputs for evaluating model selection

    mock_atom_dataset = MockAtomDataset(10)

    atom_inputs = [mock_atom_dataset[0], mock_atom_dataset[1]]
    batched_atom_input = collate_inputs_to_batched_atom_input(atom_inputs, atoms_per_window=27)

    # two models to be selected

    alphafold3_kwargs = dict(
        dim_atom_inputs=77,
        dim_pairwise=8,
        dim_single=8,
        dim_token=8,
        atoms_per_window=27,
        dim_template_feats=44,
        num_dist_bins=38,
        confidence_head_kwargs=dict(pairformer_depth=1),
        template_embedder_kwargs=dict(pairformer_stack_depth=1),
        msa_module_kwargs=dict(
            depth=1,
            dim_msa=8,
        ),
        pairformer_stack=dict(
            depth=1,
            pair_bias_attn_dim_head=4,
            pair_bias_attn_heads=2,
        ),
        diffusion_module_kwargs=dict(
            atom_encoder_depth=1,
            token_transformer_depth=1,
            atom_decoder_depth=1,
            atom_decoder_kwargs=dict(attn_pair_bias_kwargs=dict(dim_head=4)),
            atom_encoder_kwargs=dict(attn_pair_bias_kwargs=dict(dim_head=4)),
        ),
    )

    alphafold3_one = Alphafold3(**alphafold3_kwargs)
    alphafold3_two = Alphafold3(**alphafold3_kwargs)

    alphafolds = (alphafold3_one, alphafold3_two)

    # evaluate

    compute_model_selection_score = ComputeModelSelectionScore()

    details = compute_model_selection_score(alphafolds, batched_atom_input, return_details=True)

    best_alphafold_by_lddt = alphafolds[details.best_lddt_index]
    assert isinstance(best_alphafold_by_lddt, Alphafold3)


def test_unresolved_protein_rasa():
    """Test the unresolved protein relative solvent accessible surface area (RASA) calculation."""
    mmcif_filepath = os.path.join("data", "test", f"{DATA_TEST_PDB_ID}-assembly1.cif")
    pdb_input = PDBInput(mmcif_filepath)

    mol_input = pdb_input_to_molecule_input(pdb_input)
    atom_input = molecule_to_atom_input(mol_input)
    batched_atom_input = collate_inputs_to_batched_atom_input([atom_input], atoms_per_window=27)
    batched_atom_input_dict = batched_atom_input.model_forward_dict()

    _, _, asym_id, _, _ = batched_atom_input_dict["additional_molecule_feats"].unbind(dim=-1)

    cid = 1
    res_chem_index = get_cid_molecule_type(
        cid, asym_id[0], batched_atom_input_dict["is_molecule_types"][0]
    )

    # NOTE: we currently only support unresolved protein calculations
    assert res_chem_index == IS_PROTEIN

    unresolved_residue_mask = torch.randint(0, 2, asym_id.shape).bool()

    compute_model_selection_score = ComputeModelSelectionScore()

    if not compute_model_selection_score.can_calculate_unresolved_protein_rasa:
        pytest.skip("`mkdssp` is not available for calculating unresolved protein RASA.")

    unresolved_rasa = compute_model_selection_score.compute_unresolved_rasa(
        unresolved_cid=[1],
        unresolved_residue_mask=unresolved_residue_mask,
        asym_id=asym_id,
        molecule_ids=batched_atom_input_dict["molecule_ids"],
        molecule_atom_lens=batched_atom_input_dict["molecule_atom_lens"],
        atom_pos=batched_atom_input_dict["atom_pos"],
        atom_mask=~batched_atom_input_dict["missing_atom_mask"],
    )

    assert exists(unresolved_rasa)


def test_readme1():
    """Test the first README example."""
    alphafold3 = Alphafold3(dim_atom_inputs=77, dim_template_feats=44)

    # mock inputs

    seq_len = 16
    molecule_atom_lens = torch.randint(1, 3, (2, seq_len))
    atom_seq_len = molecule_atom_lens.sum(dim=-1).amax()

    atom_inputs = torch.randn(2, atom_seq_len, 77)
    atompair_inputs = torch.randn(2, atom_seq_len, atom_seq_len, 5)

    additional_molecule_feats = torch.randint(0, 2, (2, seq_len, 5))
    additional_token_feats = torch.randn(2, seq_len, 33)
    is_molecule_types = torch.randint(0, 2, (2, seq_len, 5)).bool()
    is_molecule_mod = torch.randint(0, 2, (2, seq_len, 4)).bool()
    molecule_ids = torch.randint(0, 32, (2, seq_len))

    template_feats = torch.randn(2, 2, seq_len, seq_len, 44)
    template_mask = torch.ones((2, 2)).bool()

    msa = torch.randn(2, 7, seq_len, 32)
    msa_mask = torch.ones((2, 7)).bool()

    additional_msa_feats = torch.randn(2, 7, seq_len, 2)

    # required for training, but omitted on inference

    atom_pos = torch.randn(2, atom_seq_len, 3)

    molecule_atom_indices = molecule_atom_lens - 1  # last atom, as an example
    molecule_atom_indices += molecule_atom_lens.cumsum(dim=-1) - molecule_atom_lens

    distance_labels = torch.randint(0, 37, (2, seq_len, seq_len))
    resolved_labels = torch.randint(0, 2, (2, atom_seq_len))

    # train

    loss = alphafold3(
        num_recycling_steps=2,
        atom_inputs=atom_inputs,
        atompair_inputs=atompair_inputs,
        molecule_ids=molecule_ids,
        molecule_atom_lens=molecule_atom_lens,
        additional_molecule_feats=additional_molecule_feats,
        additional_msa_feats=additional_msa_feats,
        additional_token_feats=additional_token_feats,
        is_molecule_types=is_molecule_types,
        is_molecule_mod=is_molecule_mod,
        msa=msa,
        msa_mask=msa_mask,
        templates=template_feats,
        template_mask=template_mask,
        atom_pos=atom_pos,
        molecule_atom_indices=molecule_atom_indices,
        distance_labels=distance_labels,
        resolved_labels=resolved_labels,
    )

    loss.backward()

    # after much training ...

    sampled_atom_pos = alphafold3(
        num_recycling_steps=4,
        num_sample_steps=16,
        atom_inputs=atom_inputs,
        atompair_inputs=atompair_inputs,
        molecule_ids=molecule_ids,
        molecule_atom_lens=molecule_atom_lens,
        additional_molecule_feats=additional_molecule_feats,
        additional_msa_feats=additional_msa_feats,
        additional_token_feats=additional_token_feats,
        is_molecule_types=is_molecule_types,
        is_molecule_mod=is_molecule_mod,
        msa=msa,
        msa_mask=msa_mask,
        templates=template_feats,
        template_mask=template_mask,
    )

    sampled_atom_pos.shape  # (2, <atom_seqlen>, 3)
    assert sampled_atom_pos.ndim == 3


def test_readme2():
    """Test the second README example."""
    contrived_protein = "AG"

    mock_atompos = [
        torch.randn(5, 3),  # alanine has 5 non-hydrogen atoms
        torch.randn(4, 3),  # glycine has 4 non-hydrogen atoms
    ]

    train_alphafold3_input = Alphafold3Input(proteins=[contrived_protein], atom_pos=mock_atompos)

    eval_alphafold3_input = Alphafold3Input(proteins=[contrived_protein])

    batched_atom_input = alphafold3_inputs_to_batched_atom_input(
        train_alphafold3_input, atoms_per_window=27
    )

    # training

    alphafold3 = Alphafold3(
        dim_atom_inputs=3,
        dim_atompair_inputs=5,
        atoms_per_window=27,
        dim_template_feats=44,
        num_dist_bins=38,
        num_molecule_mods=0,
        confidence_head_kwargs=dict(pairformer_depth=1),
        template_embedder_kwargs=dict(pairformer_stack_depth=1),
        msa_module_kwargs=dict(depth=1),
        pairformer_stack=dict(depth=2),
        diffusion_module_kwargs=dict(
            atom_encoder_depth=1,
            token_transformer_depth=1,
            atom_decoder_depth=1,
        ),
    )

    loss = alphafold3(**batched_atom_input.model_forward_dict())
    loss.backward()

    # sampling

    batched_eval_atom_input = alphafold3_inputs_to_batched_atom_input(
        eval_alphafold3_input, atoms_per_window=27
    )

    alphafold3.eval()
    sampled_atom_pos = alphafold3(**batched_eval_atom_input.model_forward_dict())

    assert sampled_atom_pos.shape == (1, (5 + 4), 3)
