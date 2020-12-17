from dedalus.tools.cache import CachedAttribute
import logging
from dedalus.core.field import Field
from dedalus.core.evaluator import Evaluator
from dedalus.core.system import FieldSystem
from dedalus.tools.post import merge_process_files
import dedalus.public as de
import matplotlib.pyplot as plt
import numpy as np
from scipy.interpolate import interp1d
import scipy.sparse.linalg
from . import tools

logger = logging.getLogger(__name__.split('.')[-1])

class Eigenproblem():
    def __init__(self, EVP, reject=True, factor=1.5, scales=1, drift_threshold=1e6, use_ordinal=False, grow_func=lambda x: x.real, freq_func=lambda x: x.imag):
        """
        EVP is dedalus EVP object
        """
        self.reject = reject
        self.factor = factor
        self.EVP = EVP
        self.solver = EVP.build_solver()
        if self.reject:
            self._build_hires()

        self.grid_name = self.EVP.domain.bases[0].name
        self.evalues = None
        self.evalues_low = None
        self.evalues_high = None
        self.drift_threshold = drift_threshold
        self.use_ordinal = use_ordinal
        self.scales = scales
        self.grow_func = grow_func
        self.freq_func = freq_func

    def _set_parameters(self, parameters):
        """set the parameters in the underlying EVP object

        """
        for k,v in parameters.items():
            tools.update_EVP_params(self.EVP, k, v)
            if self.reject:
                tools.update_EVP_params(self.EVP_hires, k, v)

    def grid(self):
        return self.EVP.domain.grids(scales=self.scales)[0]

    def solve(self, sparse=False, parameters=None, pencil=0, N=15, target=0, **kwargs):
        if parameters:
            self._set_parameters(parameters)
        self.pencil = pencil
        self.N = N
        self.target = target
        self.solver_kwargs = kwargs

        self._run_solver(self.solver, sparse)
        self.evalues_low = self.solver.eigenvalues

        if self.reject:
            self._run_solver(self.hires_solver, sparse)
            self.evalues_high = self.hires_solver.eigenvalues
            self._reject_spurious()
        else:
            self.evalues = self.evalues_lowres
            self.evalues_index = np.arange(len(self.evalues),dtype=int)

    def _run_solver(self, solver, sparse):
        if sparse:
            solver.solve_sparse(solver.pencils[self.pencil], N=self.N, target=self.target, rebuild_coeffs=True, **self.solver_kwargs)
        else:
            solver.solve_dense(solver.pencils[self.pencil], rebuild_coeffs=True)

    def _set_eigenmode(self, index, all_modes=False):
        if all_modes:
            good_index = index
        else:
            good_index = self.evalues_index[index]
        self.solver.set_state(good_index)

    def eigenmode(self, index, scales=None, all_modes=False):
        """Returns Dedalus FieldSystem object containing the eigenmode given by index

        """
        self._set_eigenmode(index, all_modes=all_modes)
        if scales is not None:
            self.scales = scales
        for f in self.solver.state.fields:
            f.set_scales(self.scales,keep_data=True)

        return self.solver.state
        
    def growth_rate(self, parameters=None, **kwargs):
        """returns the growth rate, defined as the eigenvalue with the largest
        real part. May acually be a decay rate if there is no growing mode.
        
        also returns the index of the fastest growing mode.  If there are no
        good eigenvalue, returns nan, nan, nan.
        """
        try:
            self.solve(parameters=parameters, **kwargs)
            gr_rate = np.max(self.grow_func(self.evalues))
            gr_indx = np.where(self.grow_func(self.evalues) == gr_rate)[0]
            freq = self.freq_func(self.evalues[gr_indx[0]])

            return gr_rate, gr_indx[0], freq

        except np.linalg.linalg.LinAlgError:
            logger.warning("Dense eigenvalue solver failed for parameters {}".format(params))
            return np.nan, np.nan, np.nan
        except (scipy.sparse.linalg.eigen.arpack.ArpackNoConvergence, scipy.sparse.linalg.eigen.arpack.ArpackError):
            logger.warning("Sparse eigenvalue solver failed to converge for parameters {}".format(params))
            return np.nan, np.nan, np.nan

    def plot_mode(self, index, fig_height=8, norm_var=None, scales=None, all_modes=False):
        state = self.eigenmode(index, scales=scales, all_modes=all_modes)

        z = self.grid()
        nrow = 2
        nvars = len(self.EVP.variables)
        ncol = int(np.ceil(nvars/nrow))

        if norm_var:
            rotation = self.solver.state[norm_var]['g'].conj()
        else:
            rotation = 1.

        fig = plt.figure(figsize=[fig_height*ncol/nrow,fig_height])
        for i,v in enumerate(self.EVP.variables):
            ax  = fig.add_subplot(nrow,ncol,i+1)
            ax.plot(z, (rotation*state[v]['g']).real, label='real')
            ax.plot(z, (rotation*state[v]['g']).imag, label='imag')
            ax.set_xlabel(self.grid_name, fontsize=15)
            ax.set_ylabel(v, fontsize=15)
            if i == 0:
                ax.legend(fontsize=15)
                
        fig.tight_layout()

        return fig

    def project_mode(self, index, domain, transverse_modes, all_modes=False):
        """projects a mode specified by index onto a domain 

        Parameters
        ----------
        index : an integer giving the eigenmode to project
        domain : a domain to project onto
        transverse_modes : a tuple of mode numbers for the transverse directions
        """
        
        if len(transverse_modes) != (len(domain.bases) - 1):
            raise ValueError("Must specify {} transverse modes for a domain with {} bases; {} specified".format(len(domain.bases)-1, len(domain.bases), len(transverse_modes)))

        field_slice = tuple(i for i in [transverse_modes, slice(None)])

        self._set_eigenmode(index, all_modes=all_modes)

        fields = []
        
        for v in self.EVP.variables:
            fields.append(domain.new_field(name=v))
            fields[-1]['c'][field_slice] = self.solver.state[v]['c']
        field_system = FieldSystem(fields)

        return field_system
    
    def write_global_domain(self, field_system, base_name="IVP_output"):
        output_evaluator = Evaluator(field_system.domain, self.EVP.namespace)
        output_handler = output_evaluator.add_file_handler(base_name)
        output_handler.add_system(field_system)

        output_evaluator.evaluate_handlers(output_evaluator.handlers, timestep=0,sim_time=0, world_time=0, wall_time=0, iteration=0)

        merge_process_files(base_name, cleanup=True)

    def calc_ps(self, k, zgrid, mu=0.):
        """computes epsilon-pseudospectrum for the eigenproblem.
        Parameters:
        k    : int
            number of eigenmodes in invariant subspace
        zgrid : tuple
            (real, imag) points

        mu : complex
            center point for pseudospectrum. 
        """

        self.solve(sparse=True, N=k) # O(N k)?
        pre_right = self.solver.pencils[0].pre_right
        pre_right_LU = scipy.sparse.linalg.splu(pre_right.tocsc()) # O(N)
        V = pre_right_LU.solve(self.solver.eigenvectors) # O(N k)

        # Orthogonalize invariant subspace
        Q, R = np.linalg.qr(V) # O(N k^2)

        # Compute approximate Schur factor
        E = -(self.solver.pencils[0].M_exp)
        A = (self.solver.pencils[0].L_exp)
        A_mu_E = A - mu*E
        A_mu_E_LU = scipy.sparse.linalg.splu(A_mu_E.tocsc()) # O(N)
        Ghat = Q.conj().T @ A_mu_E_LU.solve(E @ Q) # O(N k^2)

        # Invert-shift Schur factor
        I = np.identity(k)
        Gmu = np.linalg.inv(Ghat) + mu*I # O(k^3)

        R = self._pseudo(Gmu, zgrid)
        self.pseudospectrum = R
        self.ps_real = zgrid[0]
        self.ps_imag = zgrid[1]
        
    def _pseudo(self, L, zgrid, norm=-2):
        """computes epsilon-pseudospectrum for a regular eigenvalue problem.

        Uses resolvent; First definition (eq. 2.1) from Trefethen & Embree (1999)

        sigma_eps = ||(z*I - L)**-1|| > eps**-1

        By default uses 2-norm.

        Parameters
        ----------
        L : square 2D ndarray
            the matrix to be analyzed
        zgrid : tuple
            (real, imag) points
        """
        xx = zgrid[0]
        yy = zgrid[1]
        R = np.zeros((len(xx), len(yy)))
        matsize = L.shape[0]
        for j, y in enumerate(yy):
            for i, x in enumerate(xx):
                z = x + 1j*y
                R[j, i] = np.linalg.norm((z*np.eye(matsize) - L), ord=norm)
        return R

    def spectrum(self, figtitle='eigenvalue', spectype='good', xlog=True, ylog=True, real_label="real", imag_label="imag"):
        """Plots the spectrum.

        The spectrum plots real parts on the x axis and imaginary parts on the y axis.

        Parameters
        ----------
        figtitle : str, optional
                   string to be used in output filename.
        spectype : {'good', 'low', 'high'}, optional
                   specifies whether to use good, low, or high eigenvalues
        xlog : bool, optional
               Use symlog on x axis
        ylog : bool, optional
               Use symlog on y axis
        real_label : str, optional
                     Label to be applied to the real axis
        imag_label : str, optional
                     Label to be applied to the imaginary axis
        """
        if spectype == 'low':
            ev = self.evalues_low
        elif spectype == 'high':
            ev = self.evalues_high
        elif spectype == 'good':
            ev = self.evalues_good
        else:
            raise ValueError("Spectrum type is not one of {low, high, good}")

        fig = plt.figure()
        ax = fig.add_subplot(111)
                
        ax.scatter(ev.real, ev.imag)

        if xlog:
            ax.set_xscale('symlog')
        if ylog:
            ax.set_yscale('symlog')
        ax.set_xlabel(real_label, size = 15)
        ax.set_ylabel(imag_label, size = 15)
        fig.tight_layout()
        fig.savefig('{}_spectrum_{}.png'.format(figtitle,spectype))

        return fig

    def _reject_spurious(self):
        """may be able to pull everything out of EVP to construct a new one with higher N..."""
        evg, indx = self._discard_spurious_eigenvalues()
        self.evalues_good = evg
        self.evalues_index = indx
        self.evalues = self.evalues_good

    def _build_hires(self):
        old_evp = self.EVP
        old_x = old_evp.domain.bases[0]

        x = tools.basis_from_basis(old_x, self.factor)
        d = de.Domain([x],comm=old_evp.domain.dist.comm)
        self.EVP_hires = de.EVP(d,old_evp.variables,old_evp.eigenvalue, ncc_cutoff=old_evp.ncc_kw['cutoff'], max_ncc_terms=old_evp.ncc_kw['max_terms'], tolerance=self.EVP.tol)

        for k,v in old_evp.substitutions.items():
            self.EVP_hires.substitutions[k] = v

        for k,v in old_evp.parameters.items():
            if type(v) == Field: #NCCs
                new_field = d.new_field()
                v.set_scales(self.factor, keep_data=True)
                new_field['g'] = v['g']
                self.EVP_hires.parameters[k] = new_field
            else: #scalars
                self.EVP_hires.parameters[k] = v

        for e in old_evp.equations:
            self.EVP_hires.add_equation(e['raw_equation'])

        try:
            for b in old_evp.boundary_conditions:
                self.EVP_hires.add_bc(b['raw_equation'])
        except AttributeError:
            # after version befc23584fea, Dedalus no longer
            # distingishes BCs from other equations
            pass

        self.hires_solver = self.EVP_hires.build_solver()
        
    def _discard_spurious_eigenvalues(self):
        """
        Solves the linear eigenvalue problem for two different resolutions.
        Returns trustworthy eigenvalues using nearest delta, from Boyd chapter 7.
        """
        eval_low = self.evalues_low
        eval_hi = self.evalues_high

        # Reverse engineer correct indices to make unsorted list from sorted
        reverse_eval_low_indx = np.arange(len(eval_low)) 
        reverse_eval_hi_indx = np.arange(len(eval_hi))
    
        eval_low_and_indx = np.asarray(list(zip(eval_low, reverse_eval_low_indx)))
        eval_hi_and_indx = np.asarray(list(zip(eval_hi, reverse_eval_hi_indx)))
        
        # remove nans
        eval_low_and_indx = eval_low_and_indx[np.isfinite(eval_low)]
        eval_hi_and_indx = eval_hi_and_indx[np.isfinite(eval_hi)]
    
        # Sort eval_low and eval_hi by real parts
        eval_low_and_indx = eval_low_and_indx[np.argsort(eval_low_and_indx[:, 0].real)]
        eval_hi_and_indx = eval_hi_and_indx[np.argsort(eval_hi_and_indx[:, 0].real)]
        
        eval_low_sorted = eval_low_and_indx[:, 0]
        eval_hi_sorted = eval_hi_and_indx[:, 0]

        # Compute sigmas from lower resolution run (gridnum = N1)
        sigmas = np.zeros(len(eval_low_sorted))
        sigmas[0] = np.abs(eval_low_sorted[0] - eval_low_sorted[1])
        sigmas[1:-1] = [0.5*(np.abs(eval_low_sorted[j] - eval_low_sorted[j - 1]) + np.abs(eval_low_sorted[j + 1] - eval_low_sorted[j])) for j in range(1, len(eval_low_sorted) - 1)]
        sigmas[-1] = np.abs(eval_low_sorted[-2] - eval_low_sorted[-1])

        if not (np.isfinite(sigmas)).all():
            logger.warning("At least one eigenvalue spacings (sigmas) is non-finite (np.inf or np.nan)!")
    
        # Ordinal delta
        self.delta_ordinal = np.array([np.abs(eval_low_sorted[j] - eval_hi_sorted[j])/sigmas[j] for j in range(len(eval_low_sorted))])

        # Nearest delta
        self.delta_near = np.array([np.nanmin(np.abs(eval_low_sorted[j] - eval_hi_sorted)/sigmas[j]) for j in range(len(eval_low_sorted))])
    
        # Discard eigenvalues with 1/delta_near < drift_threshold
        if self.use_ordinal:
            inverse_drift = 1/self.delta_ordinal
        else:
            inverse_drift = 1/self.delta_near
        eval_low_and_indx = eval_low_and_indx[np.where(inverse_drift > self.drift_threshold)]
        
        eval_low = eval_low_and_indx[:, 0]
        indx = eval_low_and_indx[:, 1].real.astype(np.int)
    
        return eval_low, indx

    def plot_drift_ratios(self):
        """Plot drift ratios (both ordinal and nearest) vs. mode number.

        The drift ratios give a measure of how good a given eigenmode is; this can help set thresholds.

        """
        if self.reject is False:
            raise NotImplementedError("Can't plot drift ratios unless eigenvalue rejection is True.")

        fig = plt.figure()
        ax = fig.add_subplot(111)
        mode_numbers = np.arange(len(self.delta_near))
        ax.semilogy(mode_numbers,1/self.delta_near,'o',alpha=0.4)
        ax.semilogy(mode_numbers,1/self.delta_ordinal,'x',alpha=0.4)

        ax.set_prop_cycle(None)
        good_near = 1/self.delta_near > self.drift_threshold
        good_ordinal = 1/self.delta_ordinal > self.drift_threshold
        ax.semilogy(mode_numbers[good_near],1/self.delta_near[good_near],'o', label='nearest')
        ax.semilogy(mode_numbers[good_ordinal],1/self.delta_ordinal[good_ordinal],'x',label='ordinal')
        ax.axhline(self.drift_threshold,alpha=0.4, color='black')
        ax.set_xlabel("mode number", size=15)
        ax.set_ylabel(r"$1/\delta$", size=15)
        ax.legend(fontsize=15)

        return fig
