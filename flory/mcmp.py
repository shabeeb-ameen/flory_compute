"""Module for finding coexisting phases of multicomponent mixtures

:mod:`flory.mcmp` provides tools for finding equilibrium multiple coexisting phases in
multicomponent mixtures in the canonical ensemble based on Flory-Huggins theory. The
module provides both the function API :meth:`coexisting_phases_finder` and the class API
:class:`CoexistingPhasesFinder`. The function :meth:`coexisting_phases_finder` aims at
easing the calculation of the coexisting phases with a single system setting, namely one
single point in the phase diagram. In contrast, The class :class:`CoexistingPhasesFinder`
is designed for reuse over systems with the same system sizes such as the number of
components, which includes generating a coexisting curve or sampling a phase diagram. It
creates a finder to maintain its internal data, and provides more control and diagnostics
over the iteration. see ??? for examples.

.. codeauthor:: Yicheng Qiang <yicheng.qiang@ds.mpg.de>
.. codeauthor:: David Zwicker <david.zwicker@ds.mpg.de>
"""

import logging
import numpy as np
from datetime import datetime
from typing import Optional
from tqdm.auto import tqdm
from .detail.mcmp_impl import *


class CoexistingPhasesFinder:
    """
    Create a finder for coexisting phases.
    """
    def __init__(
        self,
        chis: np.ndarray,
        phi_means: np.ndarray,
        num_compartments: int,
        *,
        sizes: Optional[np.ndarray] = None,
        rng: Optional[np.random.Generator] = None,
        max_steps: int = 1000000,
        convergence_criterion: str = "standard",
        tolerance: float = 1e-5,
        interval: int = 10000,
        progress: bool = True,
        random_std: float = 5.0,
        acceptance_Js: float = 0.0002,
        acceptance_omega: float = 0.002,
        Js_step_upper_bound: float = 0.001,
        kill_threshold: float = 0.0,
        revive_scaler: float = 1.0,
        max_revive_per_compartment: int = 16,
        additional_chis_shift: float = 1.0,
    ):
        """
        Construct a :class:`CoexistingPhasesFinder` instance for finding coexisting
        phases. This class is recommended when multiple instances of :paramref:`chis`
        matrix or :paramref:`phi_means` vector need to be calculated. The class will reuse
        all the options and the internal resources. Note that reuse the instance of this
        class is only possible when all the system sizes are not changed, including the
        number of components :math:`N_\\mathrm{c}` and the number of compartments
        :math:`M`. The the number of components :math:`N_\\mathrm{c}` is inferred from
        :paramref:`chis` and :paramref:`phi_means`, while :math:`N_\\mathrm{c}` is set by
        the parameter :paramref:`num_compartments`. Setting :paramref:`chis` matrix and
        :paramref:`phi_means` manually by the setters leads to the reset of the internal
        revive counters.

        Args:
            chis:
                The interaction matrix. 2D array with size of :math:`N_\\mathrm{c} \\times
                N_\\mathrm{c}`. This matrix should be the full :math:`\\chi` matrix of the
                system, including the solvent component. Note that the matrix must be
                symmetric, which is not checked but should be guaranteed externally.
            phi_means:
                The average volume fraction of all the components of the system. 1D array
                with size of num_components. Note that the volume fraction of the solvent
                is included as well, therefore the sum of this array must be unity, which
                is not checked by this function and should be guaranteed externally.
            num_compartments:
                Number of compartment in the system.
            sizes:
                The relative molecule volumes of the components. 1D array with size of
                num_components. This sizes vector should be the full sizes vector of the
                system, including the solvent component. None indicates a all-one vector.
                
            rng:
                Random number generator for initialization and reviving. None indicates
                that a new random number generator should be created by the class, seeded
                by current timestamp. 
            max_steps:
                The maximum number of steps in each run to find the coexisting phases.
                Default to 100000.
            convergence_criterion:
                The criterion to determine convergence. Currently "standard" is the only
                option, which requires checking of incompressibility, field error between
                successive intervals and relative volume error between successive
                intervals. 
            tolerance:
                The tolerance to determine convergence. 
            interval:
                The interval of steps to check convergence. 
            progress:
                Whether to show status when checking convergence. 
            random_std:
                The amplitude of the randomly generated fields. 
            acceptance_Js:
                The acceptance of Js. This value determines the amount of changes accepted
                in each step for the Js field. Typically this value can take the order of
                10^-3, or smaller when the system becomes larger or stiffer.
            Js_step_upper_bound:
                The maximum change of Js per step. This values determines the maximum
                amount of changes accepted in each step for the Js field. If the intended
                amount is larger this value, the changes will be scaled down to guarantee
                that the maximum changes do not exceed this value. Typically this value
                can take the order of 10^-3, or smaller when the system becomes larger or
                stiffer. 
            acceptance_omega:
                The acceptance of omegas. This value determines the amount of changes
                accepted in each step for the omega field. Note that if the iteration of
                Js is scaled down due to parameter `Js_step_upper_bound`, the iteration of
                omega fields will be scaled down simultaneously. Typically this value can
                take the order of 10^-2, or smaller when the system becomes larger or
                stiffer. 
            kill_threshold:
                The threshold of the Js for a compartment to be killed. Should be not less
                than 0. In each iteration step, the Js array will be checked, for each
                element smaller than this parameter, the corresponding compartment will be
                killed and 0 will be assigned to the corresponding mask. The dead
                compartment may be revived, depending whether reviving is allowed or
                whether the `revive_tries` has been exhausted. 
            revive_scaler:
                The factor for the conjugate fields when a dead compartment is revived.
                This value determines the range of the random conjugate field generated by
                the algorithm. Typically 1.0 or a value slightly larger than 1.0 will be a
                reasonable choice. 
            max_revive_per_compartment:
                Number of tries per compartment to revive the dead compartment. 0 or
                negative value indicates no reviving. When this value is exhausted, the
                revive will be turned off. 
            additional_chis_shift:
                Shift of the entire chis matrix to improve the convergence.
        """
        self._logger = logging.getLogger(self.__class__.__name__)

        # chis
        chis = np.array(chis)
        if chis.shape[0] == chis.shape[1]:
            self._chis = chis
            self._num_components = chis.shape[0]
            self._logger.info(
                f"We infer that there are {self._num_components} components in the system from the chis matrix."
            )
        else:
            self._logger.error(f"chis matrix with size of {chis.shape} is not square.")
            raise ValueError("chis matrix must be square.")

        # phi_means
        phi_means = np.array(phi_means)
        if phi_means.shape[0] == self._num_components:
            self._phi_means = phi_means
            if np.abs(self._phi_means.sum() - 1.0) > 1e-12:
                self._logger.warn(
                    f"Total phi_means is not 1.0. Iteration may never converge."
                )
        else:
            self._logger.error(
                f"phi_means vector with size of {phi_means.shape} is invalid, since {self._num_components} is defined by chis matrix."
            )
            raise ValueError(
                "phi_means vector must imply same component number as chis matrix."
            )

        self._num_compartments = num_compartments

        ## optional arguments

        # sizes
        if sizes is None:
            self._sizes = np.ones(self._num_components)
        else:
            sizes = np.array(sizes)
            if sizes.shape[0] == self._num_components:
                self._sizes = sizes
                if np.sum(self._sizes <= 0):
                    self._logger.warn(
                        f"Non-positive sizes detected. Iteration will probably fail."
                    )
            else:
                self._logger.error(
                    f"sizes vector with size of {sizes.shape} is invalid, since {self._num_components} is defined by chis matrix."
                )
                raise ValueError(
                    "sizes vector must imply same component number as chis matrix."
                )

        # rng
        if rng is None:
            self._rng_is_external = False
            self._rng_seed = int(datetime.now().timestamp())
            self._rng = np.random.default_rng(self._rng_seed)
        else:
            self._rng_is_external = True
            self._rng_seed = int(0)
            self._rng = rng

        # other parameters
        self._max_steps = max_steps
        self._convergence_criterion = convergence_criterion
        self._tolerance = tolerance
        self._interval = interval
        self._progress = progress

        self._random_std = random_std
        self._acceptance_Js = acceptance_Js
        self._acceptance_omega = acceptance_omega
        self._Js_step_upper_bound = Js_step_upper_bound
        self._kill_threshold = kill_threshold
        self._revive_scaler = revive_scaler
        self._max_revive_per_compartment = max_revive_per_compartment
        self._additional_chis_shift = additional_chis_shift

        ## initialize derived internal states
        self._Js = np.full(self._num_compartments, 0.0, float)
        self._omegas = np.full((self._num_components, self._num_compartments), 0.0, float)
        self._phis = np.full((self._num_components, self._num_compartments), 0.0, float)
        self._revive_count_left = (
            self._max_revive_per_compartment * self._num_compartments
        )
        self.reinitialize_random()

    def reinitialize_random(self):
        """
        Reinitialize the internal conjugate field randomly.
        """
        self._omegas = self._rng.normal(
            0.0,
            self._random_std,
            (self._num_components, self._num_compartments),
        )
        self._Js = np.full(self._num_compartments, 1.0, float)
        self._revive_count_left = (
            self._max_revive_per_compartment * self._num_compartments
        )

    def reinitialize_from_omegas(self, omegas: np.ndarray):
        """
        Reinitialize the internal conjugate field omegas from input conjugate fields.

        Args:
            omegas: 
                New omegas field, must have the same size of (num_component, num_compartment)
        """
        omegas = np.ndarray(omegas)
        if omegas.shape == self._omegas.shape:
            self._omegas = omegas
        else:
            self._logger.error(
                f"new omegas with size of {omegas.shape} is invalid. It must have the size of {(self._num_components, self._num_compartments)}."
            )
            raise ValueError("New omegas must match the size of the old one.")
        self._Js = np.ones_like(self._Js)
        self._revive_count_left = (
            self._max_revive_per_compartment * self._num_compartments
        )

    def reinitialize_from_phis(self, phis):
        """
        Reinitialize the internal conjugate field omegas from input volume fraction field.

        Args:
            phis: 
                New phis field, must have the same size of (num_component, num_compartment)
        """
        if phis.shape == self._omegas.shape:
            self._omegas = -np.log(phis)
            for itr in range(self._num_components):
                self._omegas[itr] /= self._sizes[itr]
        else:
            self._logger.error(
                f"phis with size of {phis.shape} is invalid. It must have the size of {(self._num_components, self._num_compartments)}."
            )
            raise ValueError("phis must match the size of the omegas.")
        self._Js = np.ones_like(self._Js)
        self._revive_count_left = (
            self._max_revive_per_compartment * self._num_compartments
        )

    @property
    def chis(self):
        """_summary_

        Returns:
            _type_: _description_
        """
        return self._chis

    @chis.setter
    def chis(self, chis_new: np.ndarray):
        """
        Reset interaction matrix :member:`chis`. Note that this implies implicit reset of number
        of revives, but not internal volume fractions and conjugate fields.
        """
        chis_new = np.array(chis_new)
        if chis_new.shape == self._chis.shape:
            self._chis = chis_new
        else:
            self._logger.error(
                f"new chis with size of {chis_new.shape} is invalid. It must have the size of {self._chis.shape}."
            )
            raise ValueError("New chis must match the size of the old one.")
        self._revive_count_left = (
            self._max_revive_per_compartment * self._num_compartments
        )

    @property
    def phi_means(self):
        """_summary_

        Returns:
            _type_: _description_
        """
        return self._phi_means

    @phi_means.setter
    def phi_means(self, phi_means_new):
        """
        Reset mean volume fractions `phi_means`. Note that this implies implicit reset of
        number of revives, but not internal volume fractions and conjugate fields.
        """
        phi_means_new = np.array(phi_means_new)
        if phi_means_new.shape == self._phi_means.shape:
            self._phi_means = phi_means_new
        else:
            self._logger.error(
                f"new phi_means with size of {phi_means_new.shape} is invalid. It must have the size of {self._phi_means.shape}."
            )
            raise ValueError("New phi_means must match the size of the old one.")
        self._revive_count_left = (
            self._max_revive_per_compartment * self._num_compartments
        )

    @property
    def sizes(self):
        """_summary_

        Returns:
            _type_: _description_
        """
        return self._sizes

    @sizes.setter
    def sizes(self, sizes_new):
        """
        Reset relative molecular volumes `sizes`. Note that this implies implicit reset of
        number of revives, but not internal volume fractions and conjugate fields.
        """
        sizes_new = np.array(sizes_new)
        if sizes_new.shape == self._sizes.shape:
            self._sizes = sizes_new
        else:
            self._logger.error(
                f"new sizes with size of {sizes_new.shape} is invalid. It must have the size of {self._sizes.shape}."
            )
            raise ValueError("New sizes must match the size of the old one.")
        self._revive_count_left = (
            self._max_revive_per_compartment * self._num_compartments
        )

    def run(
        self,
        *,
        max_steps: Optional[float] = None,
        tolerance: Optional[float] = None,
        interval: Optional[int] = None,
        progress: Optional[bool] = None,
    ) -> tuple[np.ndarray, np.ndarray]:
        """
        Run instance to find coexisting phases. All keywords arguments can be used to
        temporarily overwrite the values during construction of the class.

        Args:
            max_steps:
                The maximum number of steps to find the coexisting phases.
            tolerance:
                The tolerance to determine convergence. None indicates that the default
                value will be used. 
            interval:
                The interval of steps to check convergence. 
            progress:
                Flag determining whether to show a progress bar during the simulation.                

        Returns:
            [0]:
                Volume fractions of components in each phase. 2D array with the size of
                num_phases-by-num_components. 
            [1]:
                Volume fractions of each phase. 1D array with the size of num_phases
        """

        if max_steps is None:
            max_steps = self._max_steps
        if tolerance is None:
            tolerance = self._tolerance
        if interval is None:
            interval = self._interval
        if progress is None:
            progress = self._progress

        steps_tracker = int(np.ceil(max_steps / interval))
        steps_inner = max(1, int(np.ceil(max_steps)) // steps_tracker)

        steps = 0

        chis_shifted = self._chis.copy()
        chis_shifted += -self._chis.min() + self._additional_chis_shift

        bar_max = -np.log10(tolerance)
        bar_format = "{desc:<20}: {percentage:3.0f}%|{bar}{r_bar}"
        bar_args = {
            "total": bar_max,
            "disable": not progress,
            "bar_format": bar_format,
        }
        pbar1 = tqdm(**bar_args, position=0, desc="Incompressibility")
        pbar2 = tqdm(**bar_args, position=1, desc="Field Error")
        pbar3 = tqdm(**bar_args, position=2, desc="Volume Error")

        bar_val_func = lambda a: max(0, min(round(-np.log10(max(a, 1e-100)), 1), bar_max))

        for _ in range(steps_tracker):
            # do the inner steps
            (
                max_abs_incomp,
                max_abs_omega_diff,
                max_abs_Js_diff,
                revive_count,
                is_last_step_safe,
            ) = multicomponent_self_consistent_metastep(
                self._phi_means,
                chis_shifted,
                self._sizes,
                omegas=self._omegas,
                Js=self._Js,
                phis=self._phis,
                steps_inner=steps_inner,
                acceptance_Js=self._acceptance_Js,
                Js_step_upper_bound=self._Js_step_upper_bound,
                acceptance_omega=self._acceptance_omega,
                kill_threshold=self._kill_threshold,
                revive_tries=self._revive_count_left,
                revive_scaler=self._revive_scaler,
                rng=self._rng,
            )

            if progress:
                pbar1.n = bar_val_func(max_abs_incomp)
                pbar2.n = bar_val_func(max_abs_omega_diff)
                pbar3.n = bar_val_func(max_abs_Js_diff)
                pbar1.refresh()
                pbar2.refresh()
                pbar3.refresh()

            steps += steps_inner
            self._revive_count_left -= revive_count

            # check convergence
            if self._convergence_criterion == "standard":
                if (
                    is_last_step_safe
                    and tolerance > max_abs_incomp
                    and tolerance > max_abs_omega_diff
                    and tolerance > max_abs_Js_diff
                ):
                    self._logger.info(
                        f"Composition and volumes reached stationary state after {steps} steps"
                    )
                    break
            else:
                raise ValueError(
                    f"Undefined convergence criterion: {self._convergence_criterion}"
                )

        pbar1.close()
        pbar2.close()
        pbar3.close()

        # get final result
        final_Js = self._Js.copy()
        final_phis = self._phis.copy()
        revive_compartments_by_copy(final_Js, final_phis, self._kill_threshold, self._rng)

        # store diagnostic output
        self._diagnostics = {
            "steps": steps,
            "max_abs_incomp": max_abs_incomp,
            "max_abs_omega_diff": max_abs_omega_diff,
            "max_abs_js_diff": max_abs_Js_diff,
            "revive_count_left": self._revive_count_left,
            "phis": final_phis,
            "Js": final_Js,
        }

        phases_volumes, phases_compositions = get_clusters(final_Js, final_phis)

        return phases_volumes, phases_compositions


def coexisting_phases_finder(
    chis: np.ndarray,
    phi_means: np.ndarray,
    num_compartments: int,
    **kwargs,
) -> tuple[np.ndarray, np.ndarray]:
    """
    The convenience version of `CoexistingPhasesFinder`. This function will create the
    class `CoexistingPhasesFinder` internally, and then conduct the random initialization,
    finally use self consistent iterations to find coexisting phases. `kwargs` is
    forwarded to `CoexistingPhasesFinder`. See class `CoexistingPhasesFinder` for all the
    possible options.

    Args:
        chis:
            The interaction matrix. 2D array with size of num_components-by-num_components. This chi
            matrix should be the full chi matrix of the system, including the solvent
            component. Note that the symmetry is not checked, which should be guaranteed
            externally.
        phi_means:
            The average volume fraction of all the components of the system. 1D array with
            size of num_components. Note that the volume fraction of the solvent is included as
            well, therefore the sum of this array must be unity, which is not checked by
            this function and should be guaranteed externally.
        num_compartments:
            Number of compartment in the system.

    Returns:         
        [0]:
            Volume fractions of components in each phase. 2D array with the size of num_phases-by-num_components 
        [1]:
            Volume fractions of each phase. 1D array with the size of num_phases
    """
    finder = CoexistingPhasesFinder(chis, phi_means, num_compartments, **kwargs)
    return finder.run()
