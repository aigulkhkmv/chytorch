# -*- coding: utf-8 -*-
#
#  Copyright 2022 Ramil Nugmanov <nougmanoff@protonmail.com>
#  This file is part of chytorch.
#
#  chytorch is free software; you can redistribute it and/or modify
#  it under the terms of the GNU Lesser General Public License as published by
#  the Free Software Foundation; either version 3 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
#  GNU Lesser General Public License for more details.
#
#  You should have received a copy of the GNU Lesser General Public License
#  along with this program; if not, see <https://www.gnu.org/licenses/>.
#
from chython import ReactionContainer
from math import inf
from torch import IntTensor, cat, zeros, int32, Size, float32
from torch.nn.utils.rnn import pad_sequence
from torch.utils.data import Dataset
from torchtyping import TensorType
from typing import Sequence, Tuple, Union
from ..molecule import MoleculeDataset


def collate_decoded_reactions(batch) -> Tuple[TensorType['batch*2', 'atoms', int],
                                              TensorType['batch*2', 'atoms', int],
                                              TensorType['batch*2', 'atoms', 'atoms', int],
                                              TensorType['batch', 'atoms', float]]:
    """
    Prepares batches of reactions.

    :return: atoms, neighbors, distances, reactants atoms padding mask.
    """
    atoms, neighbors, distances, masks = [], [], [], []
    for ar, nr, dr, ap, np, dp, m in batch:
        atoms.append(ar)
        neighbors.append(nr)
        distances.append(dr)

        atoms.append(ap)
        neighbors.append(np)
        distances.append(dp)

        masks.append(m)

    pa = pad_sequence(atoms, True)
    b, s = pa.shape
    tmp = zeros(b, s, s, dtype=int32)
    tmp[:, :, 0] = 1  # prevent nan in MHA softmax on padding
    for n, d in enumerate(distances):
        s = d.size(0)
        tmp[n, :s, :s] = d
    return pa, pad_sequence(neighbors, True), tmp, pad_sequence(masks, True, -inf)


class ReactionDecoderDataset(Dataset):
    def __init__(self, reactions: Sequence[Union[ReactionContainer, bytes]], *, max_distance: int = 10,
                 add_cls: bool = True, add_molecule_cls: bool = True, symmetric_cls: bool = True,
                 disable_components_interaction: bool = False, hide_molecule_cls: bool = True, unpack: bool = False):
        """
        convert reactions to tuple of:
            atoms, neighbors and distances tensors similar to molecule dataset.
             distances - merged molecular distances matrices filled by zero for isolating attention.
            roles: 2 reactants, 3 products, 0 padding, 1 cls token.

        :param reactions: map-like reactions collection
        :param max_distance: set distances greater than cutoff to cutoff value
        :param add_cls: add special token at first position of products
        :param add_molecule_cls: add special token at first position of each molecule
        :param symmetric_cls: do bidirectional attention of molecular cls to atoms and back
        :param disable_components_interaction: treat molecule components as isolated molecules
        :param hide_molecule_cls: disable attention of products atoms to reactants molecule cls tokens
        """
        if not add_molecule_cls:
            assert not hide_molecule_cls, 'add_molecule_cls should be True if hide_molecule_cls is True'
            assert not symmetric_cls, 'add_molecule_cls should be True if symmetric_cls is True'
        self.reactions = reactions
        self.max_distance = max_distance
        self.add_cls = add_cls
        self.add_molecule_cls = add_molecule_cls
        self.symmetric_cls = symmetric_cls
        self.disable_components_interaction = disable_components_interaction
        self.hide_molecule_cls = hide_molecule_cls
        self.unpack = unpack

    def __getitem__(self, item: int) -> Tuple[TensorType['atoms', int], TensorType['atoms', int],
                                              TensorType['atoms', 'atoms', int],
                                              TensorType['atoms', int], TensorType['atoms', int],
                                              TensorType['atoms', 'atoms', int],
                                              TensorType['atoms', float]]:
        rxn = self.reactions[item]
        if self.unpack:
            rxn = ReactionContainer.unpack(rxn)
        molecules = MoleculeDataset(rxn.reactants + rxn.products, max_distance=self.max_distance,
                                    add_cls=self.add_molecule_cls, symmetric_cls=self.symmetric_cls,
                                    disable_components_interaction=self.disable_components_interaction)
        r_atoms, r_neighbors, r_distances = [], [], []
        for i in range(len(rxn.reactants)):
            a, n, d = molecules[i]
            r_atoms.append(a)
            r_neighbors.append(n)
            r_distances.append(d)

        if self.add_cls:
            # disable rxn cls in molecules encoder
            p_atoms, p_neighbors = [IntTensor([1])], [IntTensor([0])]
        else:
            p_atoms, p_neighbors = [], []
        p_distances = []
        for i in range(len(rxn.reactants), len(molecules)):
            a, n, d = molecules[i]
            p_atoms.append(a)
            p_neighbors.append(n)
            p_distances.append(d)

        r_atoms = cat(r_atoms)
        p_atoms = cat(p_atoms)

        # prevent size mismatch of mask and padded batch
        rs = r_atoms.size(0)
        ps = p_atoms.size(0)
        # reactant padding mask
        if rs < ps:
            mask = zeros(ps, dtype=float32)
            mask[rs:] = -inf  # mask padding
        else:
            mask = zeros(rs, dtype=float32)

        # fill distance matrix diagonally
        tmp = zeros(rs, rs, dtype=int32)
        i = 0
        for d in r_distances:
            if self.hide_molecule_cls:
                # disable attention of products atoms to reactants cls tokens
                mask[i] = -inf
            j = i + d.size(0)
            tmp[i:j, i:j] = d
            i = j
        r_distances = tmp

        tmp = zeros(ps, ps, dtype=int32)
        if self.add_cls:
            # add attention of rxn cls to products atoms
            # it's learnable and can separate products from reactants by bias
            tmp[0, :] = 1
            i = 1
        else:
            i = 0
        for d in p_distances:
            if self.hide_molecule_cls:
                tmp[0, i] = 0
            j = i + d.size(0)
            tmp[i:j, i:j] = d
            i = j
        p_distances = tmp
        return r_atoms, cat(r_neighbors), r_distances, p_atoms, cat(p_neighbors), p_distances, mask

    def __len__(self):
        return len(self.reactions)

    def size(self, dim):
        if dim == 0:
            return len(self.reactions)
        elif dim is None:
            return Size((len(self.reactions),))
        raise IndexError


__all__ = ['ReactionDecoderDataset', 'collate_decoded_reactions']
