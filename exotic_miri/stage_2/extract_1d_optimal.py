import numpy as np
from jwst import datamodels
from jwst.stpipe import Step
import matplotlib.pyplot as plt
from scipy.optimize import curve_fit


class Extract1DOptimalStep(Step):
    """ Optimal extraction step.
    This steps enables the user extract 1d stellar spectra using
    optimal extraction.
    """

    spec = """
    median_spatial_profile = boolean(default=False)  # use median spatial profile.
    trace_position = string(default="constant")  # locate trace method.
    aperture_center = integer(default=36)  # center of aperture.
    aperture_left_width = integer(default=10)  # left-side of aperture width.
    aperture_right_width = integer(default=10)  # right-side of aperture width.
    draw_psf_fits = boolean(default=False)  # draw gauss fits to each column.
    draw_aperture = boolean(default=False)  # draw trace fits and position.
    draw_mask = boolean(default=False)  # draw trace and dq flags mask.
    draw_spectra = boolean(default=False)  # draw extracted spectra.
    """

    def process(self, input, wavelength_map, P, readnoise):
        """Execute the step.
        Parameters
        ----------
        input: JWST data model, wavelength map, spatial profile, and readnoise.
            A data model of type CubeModel, a wavelength map array,
            a spatial profile cube, and a readnoise value.
        Returns
        -------
        wavelengths, spectra, and spectra uncertainties
        """
        with datamodels.open(input) as input_model:

            # Check input model type.
            if not isinstance(input_model, datamodels.CubeModel):
                self.log.error('Input is a {} which was not expected for '
                               'Extract1DBoxStep, skipping step.'.format(
                                str(type(input_model))))
                return None, None, None

            # Define mask and spectral trace region.
            trace_mask_cube, trace_position = self._define_spectral_trace_region(
                input_model.data)
            input_model.data = input_model.data[trace_mask_cube].reshape(
                input_model.data.shape[0], input_model.data.shape[1], -1)
            input_model.err = input_model.err[trace_mask_cube].reshape(
                input_model.data.shape[0], input_model.data.shape[1], -1)
            P = P[trace_mask_cube].reshape(
                input_model.data.shape[0], input_model.data.shape[1], -1)
            if self.draw_aperture:
                self._draw_trace_mask(input_model.data, trace_mask_cube)

            # Get wavelengths on trace.
            self.log.info('Assigning wavelengths using trace center.')
            wavelengths = wavelength_map[:, int(np.nanmedian(trace_position))]

            # Extract.
            self.log.info('Optimal extraction in progress.')
            if self.median_spatial_profile:
                P = np.median(P, axis=0)
                P /= np.sum(P, axis=1)[:, np.newaxis]
                P = np.broadcast_to(
                    P[np.newaxis, :, :], shape=input_model.data.shape)
                self.log.info('Using median spatial profile.')

            # Iterate integrations.
            fs_opt = []
            var_fs_opt = []
            for int_idx in range(input_model.data.shape[0]):
                integration = input_model.data[int_idx, :, :]
                variance = input_model.err[int_idx, :, :]**2
                spatial_profile = P[int_idx, :, :]

                # Extract standard spectrum.
                f, var_f = self.extract_standard_spectra(
                    integration, variance)

                # Revise variance estimate.
                var_revised = self.revise_variance_estimates(
                    f, spatial_profile, readnoise)

                # Extract optimal spectrum.
                f_opt, var_f_opt = self.extract_optimal_spectrum(
                    integration, spatial_profile, var_revised)
                fs_opt.append(f_opt)
                var_fs_opt.append(var_f_opt)

        fs_opt = np.array(fs_opt)
        var_fs_opt = np.array(var_fs_opt)

        if self.draw_spectra:
            self._draw_extracted_spectra(wavelengths, fs_opt)

        return wavelengths, fs_opt, var_fs_opt**0.5

    def extract_standard_spectra(self, D_S, V):
        """ f and var_f as per Horne 1986 table 1 (step 4). """
        f = np.sum(D_S, axis=1)
        var_f = np.sum(V, axis=1)
        return f, var_f

    def revise_variance_estimates(self, f, P, V_0, S=0., Q=1.):
        """ V revised as per Horne 1986 table 1 (step 6). """
        V_rev = V_0 + np.abs(f[:, np.newaxis] * P + S) / Q
        return V_rev

    def extract_optimal_spectrum(self, D_S, P, V_rev):
        """ f optimal as per Horne 1986 table 1 (step 8). """
        f_opt = np.sum(P * D_S / V_rev, axis=1) / np.sum(P ** 2 / V_rev, axis=1)
        var_f_opt = np.sum(P, axis=1) / np.sum(P ** 2 / V_rev, axis=1)
        return f_opt, var_f_opt

    def _define_spectral_trace_region(self, data_cube):
        if self.trace_position == "constant":
            trace_position = np.zeros(data_cube.shape[0]) + self.aperture_center
        elif self.trace_position == "gaussian_fits":
            # Find trace position per integration with gaussian fits.
            trace_position, trace_sigmas = \
                self._find_trace_position_per_integration(data_cube)
        else:
            raise ValueError("locate_trace_method not recognised.")

        # Define trace region to be masked.
        trace_mask_cube = np.zeros(data_cube.shape).astype(bool)
        ints_mask_left_edge = np.rint(
            trace_position - self.aperture_left_width).astype(int)
        ints_mask_right_edge = np.rint(
            trace_position + self.aperture_right_width + 1).astype(int)
        for int_idx in range(trace_mask_cube.shape[0]):
            trace_mask_cube[int_idx, :, ints_mask_left_edge[
                int_idx]:ints_mask_right_edge[int_idx]] = True
        self.log.info('Trace mask made.')

        return trace_mask_cube, trace_position

    def _find_trace_position_per_integration(self, data_cube, sigma_guess=1.59):
        trace_position = []
        trace_sigmas = []
        col_pixels = np.arange(0, data_cube.shape[2], 1)
        for int_idx, int_data in enumerate(data_cube):

            # Median stack rows.
            median_row_data = np.median(int_data, axis=0)

            try:
                popt, pcov = curve_fit(
                    self._amp_gaussian, col_pixels, median_row_data,
                    p0=[np.max(median_row_data), col_pixels[np.argmax(median_row_data)],
                        sigma_guess, 0.], method='lm')
                trace_position.append(popt[1])
                trace_sigmas.append(popt[2])
                if self.draw_psf_fits:
                    self._draw_gaussian_fit(col_pixels, median_row_data, popt, pcov)
            except ValueError as err:
                self.log.warn('Gaussian fitting failed, nans present '
                              'for integration={}.'.format(int_idx))
                trace_position.append(np.nan)
                trace_sigmas.append(np.nan)
            except RuntimeError as err:
                self.log.warn('Gaussian fitting failed to find optimal trace '
                              'centre for integration={}.'.format(int_idx))
                trace_position.append(np.nan)
                trace_sigmas.append(np.nan)

        return np.array(trace_position), np.array(trace_sigmas)

    def _amp_gaussian(self, x_vals, a, mu, sigma, base=0.):
        y = a * np.exp(-(x_vals - mu)**2 / (2. * sigma**2))
        return base + y

    def _draw_gaussian_fit(self, x_data, y_data, popt, pcov):
        fig, ax1 = plt.subplots(1, 1, figsize=(9, 7))

        # Data and fit.
        ax1.scatter(x_data, y_data, s=10, c='#000000',
                    label='Data')
        xs_hr = np.linspace(np.min(x_data), np.max(x_data), 1000)
        ax1.plot(xs_hr, self._amp_gaussian(
            xs_hr, popt[0], popt[1], popt[2], popt[3]), c='#bc5090',
                 label='Gaussian fit, mean={}.'.format(popt[1]))

        # Gaussian centre and sigma.
        centre = popt[1]
        centre_err = np.sqrt(np.diag(pcov))[1]
        ax1.axvline(centre, ls='--', c='#000000')
        ax1.axvspan(centre - centre_err, centre + centre_err,
                    alpha=0.25, color='#000000')

        ax1.set_xlabel('Col pixels')
        ax1.set_ylabel('DN')
        ax1.set_title('$\mu$={}, and $\sigma$={}.'.format(
            round(popt[1], 3), round(popt[2], 3)))
        plt.tight_layout()
        plt.show()

    def _draw_trace_mask(self, data_cube, trace_mask_cube):
        for int_idx in range(data_cube.shape[0]):
            fig, (ax1, ax2, ax3) = plt.subplots(1, 3, figsize=(9, 7))
            ax1.get_shared_y_axes().join(ax1, ax2, ax3)
            ax1.get_shared_x_axes().join(ax1, ax2)

            # Data.
            im = data_cube[int_idx, :, :]
            ax1.imshow(im, origin='lower', aspect='auto', interpolation='none',
                       vmin=np.percentile(im.ravel(), 1.),
                       vmax=np.percentile(im.ravel(), 99.))

            # Mask.
            im = trace_mask_cube[int_idx, :, :]
            ax2.imshow(im, origin='lower', aspect='auto', interpolation='none')

            # Number of pixels.
            ax3.plot(np.sum(trace_mask_cube[int_idx, :, :], axis=1),
                     np.arange(trace_mask_cube.shape[1]))
            ax3.set_xlim(0, 72)

            fig.suptitle('Integration={}/{}.'.format(
                int_idx, data_cube.shape[0]))
            ax1.set_ylabel('Row pixels')
            ax2.set_ylabel('Row pixels')
            ax3.set_ylabel('Row pixels')
            ax1.set_xlabel('Col pixels')
            ax2.set_xlabel('Col pixels')
            ax3.set_xlabel('Number of pixels')
            plt.tight_layout()
            plt.show()

    def _draw_extracted_spectra(self, wavelengths, spec_box):
        fig, ax1 = plt.subplots(1, 1, figsize=(13, 5))
        for int_idx in range(spec_box.shape[0]):
            ax1.plot(wavelengths, spec_box[int_idx, :], c='#bc5090', alpha=0.02)
        ax1.set_ylabel('Electrons')
        ax1.set_xlabel('Wavelength')
        plt.tight_layout()
        plt.show()
