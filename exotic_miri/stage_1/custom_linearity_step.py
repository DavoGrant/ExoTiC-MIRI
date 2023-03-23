import numpy as np
from jwst import datamodels
from jwst.stpipe import Step
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors


class CustomLinearityStep(Step):
    """ Build custom linearity step.
    This steps enables the user to build self-cal linearity corrections.
    """

    spec = """
    draw_corrections = boolean(default=False)  # draw corrections.
    """

    def process(self, input):
        """Execute the step.
        Parameters
        ----------
        input: JWST data model
            A data model of type CubeModel.
        Returns
        -------
        JWST linearity_model
            A linearity model for override_linearity usage.
        """
        with datamodels.open(input) as input_model:

            # Check input model type.
            if not isinstance(input_model, datamodels.RampModel):
                self.log.error('Input is a {} which was not expected for '
                               'CustomLinearityStep, skipping step.'.format(
                                str(type(input_model))))
                return None

            # todo: account for saturation, other dq flags, and jumps (chicken and egg).
            # todo: could try mask based on DN level rather than linear grps range.
            # todo: also try not per amp but per col.

            groups_all = np.arange(12, 173)  # Exclude grps beyond help, e.g., final.
            groups_fit = np.arange(12, 40)
            rows = (364, 394)
            amplifier_cols = [34, 35, 36, 37, 38]
            amplifier_idxs = [2, 3, 0, 1, 2]
            amplifier_dns = [[], [], [], []]
            amplifier_fs = [[], [], [], []]
            amplifier_ccs = [[], [], [], []]
            for amp_idx, amp_col in zip(amplifier_idxs, amplifier_cols):

                # Get linear section of ramps for fitting and all for calibration.
                ramps_all = input_model.data[
                            :, groups_all, rows[0]:rows[1], amp_col]\
                            .reshape(groups_all.shape[0], -1)
                ramps_fit = input_model.data[
                            :, groups_fit, rows[0]:rows[1], amp_col]\
                            .reshape(groups_fit.shape[0], -1)

                # Fit each linear section with a linear model.
                lin_coeffs = np.polyfit(groups_fit, ramps_fit, 1)

                # Calculate linear model for all ramps.
                lin_ramps = np.matmul(
                    lin_coeffs.T, np.array([groups_all, np.ones(groups_all.shape)]))

                # Save F and DN values per amplifier.
                amplifier_dns[amp_idx].extend(ramps_all.T.ravel().tolist())
                amplifier_fs[amp_idx].extend(lin_ramps.ravel().tolist())

            # F = c0 + c1 * DN + c2 * DN**2 + c3 * DN**3 + c4 * DN**4.
            for amp_idx in range(4):
                fix_lin = True
                if fix_lin:
                    x = np.array(amplifier_dns[amp_idx])
                    y = amplifier_fs[amp_idx]

                    xx_fix = np.vstack((x, np.ones_like(x))).T
                    xx_fit = np.vstack((x**4, x**3, x**2)).T

                    p_fix = np.array([1., 0.])
                    y_fix = np.dot(p_fix, xx_fix.T)

                    p_fit = np.linalg.lstsq(xx_fit, y - y_fix, rcond=None)[0]
                    corr_coeffs = np.concatenate([p_fit, p_fix])
                else:
                    corr_coeffs = np.polyfit(amplifier_dns[amp_idx], amplifier_fs[amp_idx], 4)

                amplifier_ccs[amp_idx].extend(corr_coeffs)

            if self.draw_corrections:
                self.draw_amplifier_corrections(amplifier_idxs, amplifier_dns,
                                                amplifier_fs, amplifier_ccs)

            # Use default reference file as template for custom file.
            self.log.info('Building custom linearity datamodel.')
            linearity_ref_name = self.get_reference_file(input_model, 'linearity')
            linearity_model = datamodels.LinearityModel(linearity_ref_name)
            linearity_model.coeffs = np.zeros((5, 1024, 1032))
            linearity_model.dq = np.zeros((1024, 1032))

            for amp_idx in range(4):
                for coeff_idx, coeff in enumerate(np.flip(amplifier_ccs[amp_idx])):
                    linearity_model.coeffs[coeff_idx, :, amp_idx::4] = coeff

            # Overwrite
            linearity_model.coeffs[:, :, :4] = 0.

            linearity_model.meta.ref_file = input_model.meta.ref_file

        return linearity_model

    def finalize_result(self, res, ref):
        # Required to enable ref model to be returned.
        # Overwrites base class method.
        pass

    def linearity_correction(self, dn, coeffs):
        return coeffs[4] + coeffs[3] * dn + coeffs[2] * dn**2 \
               + coeffs[1] * dn**3 + coeffs[0] * dn**4

    def draw_amplifier_corrections(self, amplifier_idxs, amplifier_dns,
                                   amplifier_fs, amplifier_ccs):
        fig, ((ax1, ax2), (ax3, ax4)) = plt.subplots(2, 2, figsize=(11, 7))
        amp_colors = ["#003f5c", "#7a5195", "#ef5675", "#ffa600"]
        for amp_idx in range(4):

            ax1.scatter(amplifier_dns[amp_idx], amplifier_fs[amp_idx],
                        c=amp_colors[amp_idx], alpha=0.005)
            ax3.scatter(amplifier_dns[amp_idx],
                        np.array(amplifier_fs[amp_idx]) - np.array(amplifier_dns[amp_idx]),
                        c=amp_colors[amp_idx], alpha=0.005)

            dns = np.linspace(0, np.max(amplifier_dns[amp_idx]), 1000)
            ax2.plot(dns, self.linearity_correction(dns, amplifier_ccs[amp_idx]),
                     c=amp_colors[amp_idx], label="Amplifier {} correction"
                     .format(amplifier_idxs[amp_idx]))

        xs = []
        ys = []
        for amp_idx in range(4):
            for x, y in zip(amplifier_dns[amp_idx], amplifier_fs[amp_idx]):
                xs.append(x)
                ys.append(y - x)
        ax4.hexbin(xs, ys, gridsize=(30, 30), norm=mcolors.PowerNorm(gamma=0.2))

        ax1.set_xlabel("DN")
        ax1.set_ylabel("Corrected DN")

        ax3.set_xlabel("DN")
        ax3.set_ylabel("Linear model - DN")

        ax2.set_xlabel("DN")
        ax2.set_ylabel("Model corrected DN")
        ax2.set_xlim(ax1.get_xlim())
        ax2.set_ylim(ax1.get_ylim())
        ax2.legend(loc="upper left")

        ax4.set_xlabel("DN")
        ax4.set_ylabel("Linear model - DN")
        ax4.set_xlim(ax3.get_xlim())
        ax4.set_ylim(ax3.get_ylim())

        plt.tight_layout()
        plt.show()
