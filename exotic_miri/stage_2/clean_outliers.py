import warnings
import numpy as np
from jwst import datamodels
from jwst.stpipe import Step
import matplotlib.pyplot as plt
import matplotlib.patches as patches


class CleanOutliersStep(Step):
    """ Clean outliers step.
    This steps enables the user to clean outliers.
    """

    spec = """
    window_width = integer(default=100)  # window width for spatial profile fitting.
    poly_order = integer(default=4)  # spatial profile polynomial fitting order.
    outlier_threshold = float(default=4.0)  # spatial profile fitting outlier sigma.
    draw_cleaning_grid = boolean(default=False)  # draw every window cleaning.
    draw_cleaning_col = boolean(default=False)  # draw columns of window cleaning.
    """

    def __int__(self):
        self.D = None
        self.S = None
        self.V = None
        self.V0 = None
        self.G = None
        self.P = None
        self.DQ = None

    def process(self, input):
        """Execute the step.
        Parameters
        ----------
        input: JWST data model
            A data model of type CubeModel.
        Returns
        -------
        JWST data model and spatial profile cube
            A CubeModel with outliers cleaned, and a 3d np.array of the
            fitted spatial profile.
        """
        with datamodels.open(input) as input_model:

            # Copy input model.
            cleaned_model = input_model.copy()

            # Check input model type.
            if not isinstance(input_model, datamodels.CubeModel):
                self.log.error('Input is a {} which was not expected for '
                               'CleanOutliersStep, skipping step.'.format(
                                str(type(input_model))))
                return input_model

            self.D = input_model.data
            self.V = input_model.err**2
            self.P = np.empty(self.D.shape)
            self.DQ = np.ones(self.D.shape).astype(bool)

            # Clean via col-wise windowed spatial profile fitting.
            self.spatial_profile()

        cleaned_model.data = self.D
        self.DQ = self.DQ.astype(np.uint32) * 2**4
        cleaned_model.dq += self.DQ

        return cleaned_model, self.P

    def spatial_profile(self):
        """ Extract using optimal extraction method of Horne 1986. """
        # Iterate integrations.
        n_ints, n_rows, n_cols = self.D.shape
        for int_idx in range(n_ints):

            # Iterate windows of several rows.
            for win_start_idx in range(0, n_rows, self.window_width):

                # Set window in rows.
                win_end_idx = min(win_start_idx + self.window_width, n_rows)
                if win_end_idx == n_rows:
                    # If final window, set left side back so window
                    # is always the same size.
                    win_start_idx = max(win_end_idx - self.window_width, 0)

                # Construct spatial profile.
                P_win = self.construct_spatial_profile(
                    int_idx, win_start_idx, win_end_idx)

                # Normalise.
                norm_win = np.sum(P_win, axis=1)
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore', RuntimeWarning)
                    P_win /= norm_win[:, np.newaxis]
                if np.isnan(P_win).any():
                    self.log.warn(
                        'Spatial profile contains entire slice of negative '
                        'values. Setting as top hat.')
                    n_rows_win, n_cols_win = P_win.shape
                    for row_win_idx, n in enumerate(norm_win):
                        if n == 0:
                            P_win[row_win_idx, :] = 1. / n_rows_win

                # Save window of spatial profile.
                self.P[int_idx, win_start_idx:win_end_idx, :] = P_win

            self.log.info('Integration={}: cleaned {} outliers '
                          'w/ spatial profile.'.format(
                           int_idx, np.sum(~self.DQ[int_idx])))

    def construct_spatial_profile(self, int_idx, win_start_idx, win_end_idx):
        """ P as per Horne 1986 table 1 (step 5). """
        P = []
        D_S = self.D[int_idx, win_start_idx:win_end_idx, :]

        # Iterate cols in window.
        row_pixel_idxs = np.arange(D_S.shape[0])
        for col_idx in np.arange(0, D_S.shape[1]):

            D_S_col = np.copy(D_S[:, col_idx])
            col_mask = np.ones(D_S_col.shape[0]).astype(bool)
            col_mask[~np.isfinite(D_S_col)] = False  # set nans as bad.
            while True:

                try:
                    # Fit polynomial to row.
                    with warnings.catch_warnings():
                        warnings.simplefilter('ignore', np.RankWarning)
                        p_coeff = np.polyfit(
                            row_pixel_idxs[col_mask], D_S_col[col_mask],
                            self.poly_order, w=None)
                    p_row = np.polyval(p_coeff, row_pixel_idxs)
                except np.linalg.LinAlgError as err:
                    print('Poly fit error when constructing spatial profile.')
                    raise err
                except TypeError as err:
                    if np.sum(col_mask) < 2:
                        # print("Warning <2 good pixels in col window: int={}, row={}, "
                        #       "col={}. Set to zeroes.".format(
                        #       int_idx, win_start_idx, col_idx))
                        with warnings.catch_warnings():
                            warnings.simplefilter('ignore', np.RankWarning)
                            p_coeff = np.polyfit(
                                row_pixel_idxs, np.zeros(D_S_col.shape[0]),
                                self.poly_order, w=None)
                        p_row = np.polyval(p_coeff, row_pixel_idxs)
                    else:
                        raise err

                # Check residuals to polynomial fit.
                res_col = np.ma.array(D_S_col - p_row, mask=~col_mask)
                dev_col = np.ma.abs(res_col) / np.ma.std(res_col)
                max_deviation_idx = np.ma.argmax(dev_col)
                if dev_col[max_deviation_idx] > self.outlier_threshold:
                    # Outlier: mask and repeat poly fitting.
                    if self.draw_cleaning_col:
                        print('Max dev={} > threshold={}'.format(
                            dev_col[max_deviation_idx], self.outlier_threshold))
                        self.draw_poly_inter_fit(
                            int_idx, col_idx, win_start_idx, win_end_idx,
                            p_row, col_mask)

                    col_mask[max_deviation_idx] = False
                    self.DQ[int_idx, win_start_idx + max_deviation_idx,
                            col_idx] = False
                    continue
                else:
                    P.append(p_row)

                    # Replace data with poly val.
                    for win_idx, good_pix in enumerate(col_mask):
                        if not good_pix:
                            self.D[int_idx, win_start_idx + win_idx,
                                   col_idx] = np.polyval(
                                       p_coeff, win_idx)

                            # Set for nans.
                            self.DQ[int_idx, win_start_idx + win_idx,
                                    col_idx] = False

                    if self.draw_cleaning_col:
                        print('Max dev={} > threshold={}'.format(
                            dev_col[max_deviation_idx], self.outlier_threshold))
                        print('Final cleaned data and fit.')
                        self.draw_poly_inter_fit(
                            int_idx, col_idx, win_start_idx, win_end_idx,
                            p_row, final=True)
                    break

        # Enforce positivity.
        P = np.array(P).T
        P[P < 0.] = 0.

        return P

    def draw_poly_fit(self, idx_slice, x_data, y_data, x_model, y_model):
        """ Draw the polynomial fit. """
        fig, ax1 = plt.subplots(1, 1, figsize=(8, 7))
        ax1.scatter(x_data, y_data, s=10, c='#000000', alpha=0.8,
                    label='Data, slice={}.'.format(idx_slice))
        ax1.plot(x_model, y_model, c='#bc5090', lw=3,
                 label='Poly fit, order={}.'.format(self.poly_order))
        ax1.set_xlabel('Pixel')
        ax1.set_ylabel('Electrons')
        plt.legend(loc='upper center')
        plt.tight_layout()
        plt.show()

    def draw_poly_inter_fit(self, int_idx, col_idx, win_start_idx,
                            win_end_idx, p_col, col_mask=None, final=False):
        """ Draw the polynomial fit. """
        fig, (ax1, ax2, ax3) = plt.subplots(1, 3, figsize=(13, 8))
        ax1.get_shared_x_axes().join(ax1, ax2)
        ax1.get_shared_y_axes().join(ax1, ax2)

        ax1.imshow(~self.DQ[int_idx], origin='lower', aspect='auto',
                   interpolation='none')
        im = self.D[int_idx]
        ax2.imshow(im, origin='lower', aspect='auto',
                   interpolation='none',
                   vmin=np.nanpercentile(im, 0.5),
                   vmax=np.nanpercentile(im, 99.5))

        rect = patches.Rectangle(
            (col_idx - 0.5, win_start_idx - 0.5), 1, self.window_width,
            linewidth=1, edgecolor='#ffffff', facecolor='none')
        ax1.add_patch(rect)

        if not final:
            x = np.arange(win_start_idx, win_end_idx)
            y = self.D[int_idx, win_start_idx:win_end_idx, col_idx]
            ax3.scatter(y[col_mask], x[col_mask], s=10, c='#000000',
                        alpha=0.8, label='Col={}.'.format(col_idx))
            ax3.plot(p_col, x, c='#bc5090', lw=3,
                     label='Poly fit, order={}.'.format(self.poly_order))
        else:
            x = np.arange(win_start_idx, win_end_idx)
            y = self.D[int_idx, win_start_idx:win_end_idx, col_idx]
            ax3.scatter(y, x, s=10, c='#000000',
                        alpha=0.8, label='Col={}.'.format(col_idx))
            ax3.plot(p_col, x, c='#bc5090', lw=3,
                     label='Poly fit, order={}.'.format(self.poly_order))

        ax3.set_ylabel('Pixel')
        ax3.set_xlabel('Electrons')
        ax3.legend(loc='upper center')

        plt.tight_layout()
        plt.show()
