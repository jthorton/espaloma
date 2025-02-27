# =============================================================================
# IMPORTS
# =============================================================================
import rdkit
import torch
from openff.toolkit.topology import Molecule
import espaloma as esp

from openmmforcefields.generators import SystemGenerator
from simtk import openmm, unit
from simtk.openmm.app import Simulation
from simtk.unit import Quantity

# =============================================================================
# CONSTANTS
# =============================================================================
REDUNDANT_TYPES = {
    "cd": "cc",
    "cf": "ce",
    "cq": "cp",
    "pd": "pc",
    "pf": "pe",
    "nd": "nc",
}

# simulation specs
TEMPERATURE = 350 * unit.kelvin
STEP_SIZE = 1.0 * unit.femtosecond
COLLISION_RATE = 1.0 / unit.picosecond
EPSILON_MIN = 0.05 * unit.kilojoules_per_mole

# =============================================================================
# MODULE CLASSES
# =============================================================================
class LegacyForceField:
    """Class to hold legacy forcefield for typing and parameter assignment.

    Parameters
    ----------
    forcefield : string
        name and version of the forcefield.

    Methods
    -------
    parametrize()
        Parametrize a molecular system.

    typing()
        Provide legacy typing for a molecular system.

    """

    def __init__(self, forcefield="gaff-1.81"):
        self.forcefield = forcefield
        self._prepare_forcefield()

    @staticmethod
    def _convert_to_off(mol):
        import openff.toolkit

        if isinstance(mol, esp.Graph):
            return mol.mol

        elif isinstance(mol, openff.toolkit.topology.molecule.Molecule):
            return mol
        elif isinstance(mol, rdkit.Chem.rdchem.Mol):
            return Molecule.from_rdkit(mol)
        elif "openeye" in str(
            type(mol)
        ):  # because we don't want to depend on OE
            return Molecule.from_openeye(mol)

    def _prepare_forcefield(self):

        if "gaff" in self.forcefield:
            self._prepare_gaff()

        elif "smirnoff" in self.forcefield:
            # do nothing for now
            self._prepare_smirnoff()

        elif "openff" in self.forcefield:
            self._prepare_openff()

        else:
            raise NotImplementedError

    def _prepare_openff(self):

        from openff.toolkit.typing.engines.smirnoff import ForceField

        self.FF = ForceField("%s.offxml" % self.forcefield)

    def _prepare_smirnoff(self):

        from openff.toolkit.typing.engines.smirnoff import ForceField

        self.FF = ForceField("%s.offxml" % self.forcefield)

    def _prepare_gaff(self):
        import os
        import xml.etree.ElementTree as ET

        import openmmforcefields

        # get the openff.toolkits path
        openmmforcefields_path = os.path.dirname(openmmforcefields.__file__)

        # get the xml path
        ffxml_path = (
            openmmforcefields_path
            + "/ffxml/amber/gaff/ffxml/"
            + self.forcefield
            + ".xml"
        )

        # parse xml
        tree = ET.parse(ffxml_path)
        root = tree.getroot()
        nonbonded = list(root)[-1]
        atom_types = [atom.get("type") for atom in nonbonded.findall("Atom")]

        # remove redundant types
        [atom_types.remove(bad_type) for bad_type in REDUNDANT_TYPES.keys()]

        # compose the translation dictionaries
        str_2_idx = dict(zip(atom_types, range(len(atom_types))))
        idx_2_str = dict(zip(range(len(atom_types)), atom_types))

        # provide mapping for redundant types
        for bad_type, good_type in REDUNDANT_TYPES.items():
            str_2_idx[bad_type] = str_2_idx[good_type]

        # make translation dictionaries attributes of self
        self._str_2_idx = str_2_idx
        self._idx_2_str = idx_2_str

    def _type_gaff(self, g):
        """Type a molecular graph using gaff force fields."""
        # assert the forcefield is indeed of gaff family
        assert "gaff" in self.forcefield

        # make sure mol is in openff.toolkit format `
        mol = g.mol

        # import template generator
        from openmmforcefields.generators import GAFFTemplateGenerator

        gaff = GAFFTemplateGenerator(
            molecules=mol, forcefield=self.forcefield
        )

        # create temporary directory for running antechamber
        import os
        import shutil
        import tempfile

        tempdir = tempfile.mkdtemp()
        prefix = "molecule"
        input_sdf_filename = os.path.join(tempdir, prefix + ".sdf")
        gaff_mol2_filename = os.path.join(tempdir, prefix + ".gaff.mol2")
        frcmod_filename = os.path.join(tempdir, prefix + ".frcmod")

        # write sdf for input
        mol.to_file(input_sdf_filename, file_format="sdf")

        # run antechamber
        gaff._run_antechamber(
            molecule_filename=input_sdf_filename,
            input_format="mdl",
            gaff_mol2_filename=gaff_mol2_filename,
            frcmod_filename=frcmod_filename,
        )

        gaff._read_gaff_atom_types_from_mol2(gaff_mol2_filename, mol)
        gaff_types = [atom.gaff_type for atom in mol.atoms]
        shutil.rmtree(tempdir)

        # put types into graph object
        if g is None:
            g = esp.Graph(mol)

        g.nodes["n1"].data["legacy_typing"] = torch.tensor(
            [self._str_2_idx[atom] for atom in gaff_types]
        )

        return g

    def _parametrize_gaff(self, g, n_max_phases=6):
        from openmmforcefields.generators import SystemGenerator

        # define a system generator
        system_generator = SystemGenerator(
            small_molecule_forcefield=self.forcefield,
        )

        mol = g.mol
        # mol.assign_partial_charges("formal_charge")
        # create system
        sys = system_generator.create_system(
            topology=mol.to_topology().to_openmm(),
            molecules=mol,
        )

        bond_lookup = {
            tuple(idxs.detach().numpy()): position
            for position, idxs in enumerate(g.nodes["n2"].data["idxs"])
        }

        angle_lookup = {
            tuple(idxs.detach().numpy()): position
            for position, idxs in enumerate(g.nodes["n3"].data["idxs"])
        }

        torsion_lookup = {
            tuple(idxs.detach().numpy()): position
            for position, idxs in enumerate(g.nodes["n4"].data["idxs"])
        }

        improper_lookup = {
            tuple(idxs.detach().numpy()): position
            for position, idxs in enumerate(
                g.nodes["n4_improper"].data["idxs"]
            )
        }

        torsion_phases = torch.zeros(
            g.heterograph.number_of_nodes("n4"),
            n_max_phases,
        )

        torsion_periodicities = torch.zeros(
            g.heterograph.number_of_nodes("n4"),
            n_max_phases,
        )

        torsion_ks = torch.zeros(
            g.heterograph.number_of_nodes("n4"),
            n_max_phases,
        )

        improper_phases = torch.zeros(
            g.heterograph.number_of_nodes("n4"),
            n_max_phases,
        )

        improper_periodicities = torch.zeros(
            g.heterograph.number_of_nodes("n4"),
            n_max_phases,
        )

        improper_ks = torch.zeros(
            g.heterograph.number_of_nodes("n4"),
            n_max_phases,
        )

        for force in sys.getForces():
            name = force.__class__.__name__
            if "HarmonicBondForce" in name:
                assert (
                    force.getNumBonds() * 2
                    == g.heterograph.number_of_nodes("n2")
                )

                g.nodes["n2"].data["eq_ref"] = torch.zeros(
                    force.getNumBonds() * 2, 1
                )

                g.nodes["n2"].data["k_ref"] = torch.zeros(
                    force.getNumBonds() * 2, 1
                )

                for idx in range(force.getNumBonds()):
                    idx0, idx1, eq, k = force.getBondParameters(idx)

                    position = bond_lookup[(idx0, idx1)]
                    g.nodes["n2"].data["eq_ref"][position] = eq.value_in_unit(
                        esp.units.DISTANCE_UNIT,
                    )
                    g.nodes["n2"].data["k_ref"][position] = k.value_in_unit(
                        esp.units.FORCE_CONSTANT_UNIT,
                    )

                    position = bond_lookup[(idx1, idx0)]
                    g.nodes["n2"].data["eq_ref"][position] = eq.value_in_unit(
                        esp.units.DISTANCE_UNIT,
                    )
                    g.nodes["n2"].data["k_ref"][position] = k.value_in_unit(
                        esp.units.FORCE_CONSTANT_UNIT,
                    )

            if "HarmonicAngleForce" in name:
                assert (
                    force.getNumAngles() * 2
                    == g.heterograph.number_of_nodes("n3")
                )

                g.nodes["n3"].data["eq_ref"] = torch.zeros(
                    force.getNumAngles() * 2, 1
                )

                g.nodes["n3"].data["k_ref"] = torch.zeros(
                    force.getNumAngles() * 2, 1
                )

                for idx in range(force.getNumAngles()):
                    idx0, idx1, idx2, eq, k = force.getAngleParameters(idx)

                    position = angle_lookup[(idx0, idx1, idx2)]
                    g.nodes["n3"].data["eq_ref"][position] = eq.value_in_unit(
                        esp.units.ANGLE_UNIT,
                    )
                    g.nodes["n3"].data["k_ref"][position] = k.value_in_unit(
                        esp.units.ANGLE_FORCE_CONSTANT_UNIT,
                    )

                    position = angle_lookup[(idx2, idx1, idx0)]
                    g.nodes["n3"].data["eq_ref"][position] = eq.value_in_unit(
                        esp.units.ANGLE_UNIT,
                    )
                    g.nodes["n3"].data["k_ref"][position] = k.value_in_unit(
                        esp.units.ANGLE_FORCE_CONSTANT_UNIT,
                    )

            if "PeriodicTorsionForce" in name:
                for idx in range(force.getNumTorsions()):
                    (
                        idx0,
                        idx1,
                        idx2,
                        idx3,
                        periodicity,
                        phase,
                        k,
                    ) = force.getTorsionParameters(idx)

                    if (idx0, idx1, idx2, idx3) in torsion_lookup:
                        position = torsion_lookup[(idx0, idx1, idx2, idx3)]
                        for sub_idx in range(n_max_phases):
                            if torsion_ks[position, sub_idx] == 0:
                                torsion_ks[
                                    position, sub_idx
                                ] = 0.5 * k.value_in_unit(
                                    esp.units.ENERGY_UNIT
                                )
                                torsion_phases[
                                    position, sub_idx
                                ] = phase.value_in_unit(esp.units.ANGLE_UNIT)
                                torsion_periodicities[
                                    position, sub_idx
                                ] = periodicity

                                position = torsion_lookup[
                                    (idx3, idx2, idx1, idx0)
                                ]
                                torsion_ks[
                                    position, sub_idx
                                ] = 0.5 * k.value_in_unit(
                                    esp.units.ENERGY_UNIT
                                )
                                torsion_phases[
                                    position, sub_idx
                                ] = phase.value_in_unit(esp.units.ANGLE_UNIT)
                                torsion_periodicities[
                                    position, sub_idx
                                ] = periodicity
                                break

            g.heterograph.apply_nodes(
                lambda nodes: {
                    "k_ref": torsion_ks,
                    "periodicity_ref": torsion_periodicities,
                    "phases_ref": torsion_phases,
                },
                ntype="n4",
            )

            """
            g.heterograph.apply_nodes(
                    lambda nodes: {
                        "k_ref": improper_ks,
                        "periodicity_ref": improper_periodicities,
                        "phases_ref": improper_phases,
                    },
                    ntype="n4_improper"
            )

            """

        """
        def apply_torsion(node, n_max_phases=6):
            phases = torch.zeros(
                g.heterograph.number_of_nodes("n4"), n_max_phases,
            )

            periodicity = torch.zeros(
                g.heterograph.number_of_nodes("n4"), n_max_phases,
            )

            k = torch.zeros(g.heterograph.number_of_nodes("n4"), n_max_phases,)

            for idx in range(g.heterograph.number_of_nodes("n4")):
                idxs = tuple(node.data["idxs"][idx].numpy())
                if idxs in force:
                    _force = force[idxs]
                    for sub_idx in range(len(_force.periodicity)):
                        if hasattr(_force, "k%s" % sub_idx):
                            k[idx, sub_idx] = getattr(
                                _force, "k%s" % sub_idx
                            ).value_in_unit(esp.units.ENERGY_UNIT)

                            phases[idx, sub_idx] = getattr(
                                _force, "phase%s" % sub_idx
                            ).value_in_unit(esp.units.ANGLE_UNIT)

                            periodicity[idx, sub_idx] = getattr(
                                _force, "periodicity%s" % sub_idx
                            )

            return {
                "k_ref": k,
                "periodicity_ref": periodicity,
                "phases_ref": phases,
            }

        g.heterograph.apply_nodes(apply_torsion, ntype="n4")
        """

        return g

    def _parametrize_smirnoff(self, g):
        # mol = self._convert_to_off(mol)
        forces = self.FF.label_molecules(g.mol.to_topology())[0]

        g.heterograph.apply_nodes(
            lambda node: {
                "k_ref": 2.0
                * torch.Tensor(
                    [
                        forces["Bonds"][
                            tuple(node.data["idxs"][idx].numpy())
                        ].k.value_in_unit(esp.units.FORCE_CONSTANT_UNIT)
                        for idx in range(node.data["idxs"].shape[0])
                    ]
                )[:, None]
            },
            ntype="n2",
        )

        g.heterograph.apply_nodes(
            lambda node: {
                "eq_ref": torch.Tensor(
                    [
                        forces["Bonds"][
                            tuple(node.data["idxs"][idx].numpy())
                        ].length.value_in_unit(esp.units.DISTANCE_UNIT)
                        for idx in range(node.data["idxs"].shape[0])
                    ]
                )[:, None]
            },
            ntype="n2",
        )

        g.heterograph.apply_nodes(
            lambda node: {
                "k_ref": 2.0
                * torch.Tensor(  # OpenFF records 1/2k as param
                    [
                        forces["Angles"][
                            tuple(node.data["idxs"][idx].numpy())
                        ].k.value_in_unit(esp.units.ANGLE_FORCE_CONSTANT_UNIT)
                        for idx in range(node.data["idxs"].shape[0])
                    ]
                )[:, None]
            },
            ntype="n3",
        )

        g.heterograph.apply_nodes(
            lambda node: {
                "eq_ref": torch.Tensor(
                    [
                        forces["Angles"][
                            tuple(node.data["idxs"][idx].numpy())
                        ].angle.value_in_unit(esp.units.ANGLE_UNIT)
                        for idx in range(node.data["idxs"].shape[0])
                    ]
                )[:, None]
            },
            ntype="n3",
        )

        g.heterograph.apply_nodes(
            lambda node: {
                "epsilon_ref": torch.Tensor(
                    [
                        forces["vdW"][(idx,)].epsilon.value_in_unit(
                            esp.units.ENERGY_UNIT
                        )
                        for idx in range(g.heterograph.number_of_nodes("n1"))
                    ]
                )[:, None]
            },
            ntype="n1",
        )

        g.heterograph.apply_nodes(
            lambda node: {
                "sigma_ref": torch.Tensor(
                    [
                        forces["vdW"][(idx,)].rmin_half.value_in_unit(
                            esp.units.DISTANCE_UNIT
                        )
                        for idx in range(g.heterograph.number_of_nodes("n1"))
                    ]
                )[:, None]
            },
            ntype="n1",
        )

        def apply_torsion(node, n_max_phases=6):
            phases = torch.zeros(
                g.heterograph.number_of_nodes("n4"),
                n_max_phases,
            )

            periodicity = torch.zeros(
                g.heterograph.number_of_nodes("n4"),
                n_max_phases,
            )

            k = torch.zeros(
                g.heterograph.number_of_nodes("n4"),
                n_max_phases,
            )

            force = forces["ProperTorsions"]

            for idx in range(g.heterograph.number_of_nodes("n4")):
                idxs = tuple(node.data["idxs"][idx].numpy())
                if idxs in force:
                    _force = force[idxs]
                    for sub_idx in range(len(_force.periodicity)):
                        if hasattr(_force, "k%s" % sub_idx):
                            k[idx, sub_idx] = getattr(
                                _force, "k%s" % sub_idx
                            ).value_in_unit(esp.units.ENERGY_UNIT)

                            phases[idx, sub_idx] = getattr(
                                _force, "phase%s" % sub_idx
                            ).value_in_unit(esp.units.ANGLE_UNIT)

                            periodicity[idx, sub_idx] = getattr(
                                _force, "periodicity%s" % sub_idx
                            )

            return {
                "k_ref": k,
                "periodicity_ref": periodicity,
                "phases_ref": phases,
            }

        def apply_improper_torsion(node, n_max_phases=6):
            phases = torch.zeros(
                g.heterograph.number_of_nodes("n4_improper"),
                n_max_phases,
            )

            periodicity = torch.zeros(
                g.heterograph.number_of_nodes("n4_improper"),
                n_max_phases,
            )

            k = torch.zeros(
                g.heterograph.number_of_nodes("n4_improper"),
                n_max_phases,
            )

            force = forces["ImproperTorsions"]

            for idx in range(g.heterograph.number_of_nodes("n4_improper")):
                idxs = tuple(node.data["idxs"][idx].numpy())
                if idxs in force:
                    _force = force[idxs]
                    for sub_idx in range(len(_force.periodicity)):

                        if hasattr(_force, "k%s" % sub_idx):
                            k[idx, sub_idx] = getattr(
                                _force, "k%s" % sub_idx
                            ).value_in_unit(esp.units.ENERGY_UNIT)

                            phases[idx, sub_idx] = getattr(
                                _force, "phase%s" % sub_idx
                            ).value_in_unit(esp.units.ANGLE_UNIT)

                            periodicity[idx, sub_idx] = getattr(
                                _force, "periodicity%s" % sub_idx
                            )

            return {
                "k_ref": k,
                "periodicity_ref": periodicity,
                "phases_ref": phases,
            }

        g.heterograph.apply_nodes(apply_torsion, ntype="n4")
        g.heterograph.apply_nodes(apply_improper_torsion, ntype="n4_improper")

        return g

    def baseline_energy(self, g, suffix=None):
        if suffix is None:
            suffix = "_" + self.forcefield

        from openmmforcefields.generators import SystemGenerator

        # define a system generator
        system_generator = SystemGenerator(
            small_molecule_forcefield=self.forcefield,
        )

        mol = g.mol
        # mol.assign_partial_charges("formal_charge")
        # create system
        system = system_generator.create_system(
            topology=mol.to_topology().to_openmm(),
            molecules=mol,
        )

        # parameterize topology
        topology = g.mol.to_topology().to_openmm()

        integrator = openmm.LangevinIntegrator(
            TEMPERATURE, COLLISION_RATE, STEP_SIZE
        )

        # create simulation
        simulation = Simulation(
            topology=topology, system=system, integrator=integrator
        )

        us = []

        xs = (
            Quantity(
                g.nodes["n1"].data["xyz"].detach().numpy(),
                esp.units.DISTANCE_UNIT,
            )
            .value_in_unit(unit.nanometer)
            .transpose((1, 0, 2))
        )

        for x in xs:
            simulation.context.setPositions(x)
            us.append(
                simulation.context.getState(getEnergy=True)
                .getPotentialEnergy()
                .value_in_unit(esp.units.ENERGY_UNIT)
            )

        g.nodes["g"].data["u%s" % suffix] = torch.tensor(us)[None, :]

        return g

    def _multi_typing_smirnoff(self, g):
        # mol = self._convert_to_off(mol)

        forces = self.FF.label_molecules(g.mol.to_topology())[0]

        g.heterograph.apply_nodes(
            lambda node: {
                "legacy_typing": torch.Tensor(
                    [
                        int(
                            forces["Bonds"][
                                tuple(node.data["idxs"][idx].numpy())
                            ].id[1:]
                        )
                        for idx in range(node.data["idxs"].shape[0])
                    ]
                ).long()
            },
            ntype="n2",
        )

        g.heterograph.apply_nodes(
            lambda node: {
                "legacy_typing": torch.Tensor(
                    [
                        int(
                            forces["Angles"][
                                tuple(node.data["idxs"][idx].numpy())
                            ].id[1:]
                        )
                        for idx in range(node.data["idxs"].shape[0])
                    ]
                ).long()
            },
            ntype="n3",
        )

        g.heterograph.apply_nodes(
            lambda node: {
                "legacy_typing": torch.Tensor(
                    [
                        int(forces["vdW"][(idx,)].id[1:])
                        for idx in range(g.heterograph.number_of_nodes("n1"))
                    ]
                ).long()
            },
            ntype="n1",
        )

        return g

    def parametrize(self, g):
        """Parametrize a molecular graph."""
        if "smirnoff" in self.forcefield or "openff" in self.forcefield:
            return self._parametrize_smirnoff(g)

        elif "gaff" in self.forcefield:
            return self._parametrize_gaff(g)

        else:
            raise NotImplementedError

    def typing(self, g):
        """Type a molecular graph."""
        if "gaff" in self.forcefield:
            return self._type_gaff(g)

        else:
            raise NotImplementedError

    def multi_typing(self, g):
        """ Type a molecular graph for hetero nodes. """
        if "smirnoff" in self.forcefield:
            return self._multi_typing_smirnoff(g)

        else:
            raise NotImplementedError

    def __call__(self, *args, **kwargs):
        return self.typing(*args, **kwargs)
