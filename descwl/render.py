"""Render source models as simulated survey observations.
"""

import math
import inspect

import numpy as np

import galsim

class SourceNotVisible(Exception):
    """Custom exception to indicate that a source has no visible pixels above threshold.
    """
    pass

class GalaxyRenderer(object):
    """Rendering engine for a single galaxy.

    Args:
        galaxy(descwl.model.Galaxy): Model of the galaxy we will render.
        stamp(galsim.Image): Previously rendered image of this galaxy with no transforms
            applied. Zero pixels in this image are used to define a mask where all
            subsequently rendered images will have pixels values set to zero. The input
            stamp is copied and not modified. The stamp bounds determine the region of the
            full survey image that we will render into.
        survey(descwl.survey.Survey): Survey that rendered images will simulate. Used to
            determine the mapping from survey positions to stamp positions and specify
            the PSF to use.
    """
    def __init__(self,galaxy,stamp,survey):
        self.galaxy = galaxy
        self.stamp = stamp.copy()
        self.mask = (self.stamp.array == 0)
        self.dx_arcsec = 0.5*(self.stamp.bounds.xmin + self.stamp.bounds.xmax+1 -
            survey.image_width)*survey.pixel_scale
        self.dy_arcsec = 0.5*(self.stamp.bounds.ymin + self.stamp.bounds.ymax+1 -
            survey.image_height)*survey.pixel_scale
        self.psf_model = survey.psf_model
        self.gsparams = galsim.GSParams(maximum_fft_size=1<<16)
        self.last_parameters = {'dx':0.,'dy':0.,'ds':0.,'dg1':0.,'dg2':0.}

    def draw(self,df=0.,dx=0.,dy=0.,ds=0.,dg1=0.,dg2=0.):
        """
        Draw the galaxy with the specified transforms applied.

        We use :meth:`descwl.galaxy.Galaxy.get_transformed_model` to apply all
        transforms except for `df`, which we implement internally using rescaling.
        The transformed model is convolved with the survey PSF, rendered into the
        stamp specified in our constructor, and masked (zero pixels in the untransformed
        rendering are forced to zero). Repeated calls with the same transform parameters
        return a cached image immediately. The same is true if only the `df` parameter
        is changed from the last call.

        Args:
            df(float): Relative amount to scale the total galaxy flux.
            dx(float): Amount to shift centroid in x, in arcseconds.
            dy(float): Amount to shift centroid in y, in arcseconds.
            ds(float): Relative amount to scale the galaxy profile in the
                radial direction while conserving flux, before applying shear
                or convolving with the PSF.
            dg1(float): Amount to adjust the + shear applied to the galaxy profile,
                with \|g\| = (a-b)/(a+b), before convolving with the PSF.
            dg2(float): Amount to adjust the x shear applied to the galaxy profile,
                with \|g\| = (a-b)/(a+b), before convolving with the PSF.

        Returns:
            galsim.Image: Rendering of the transformed galaxy.
        """
        # We do not include df here since we implement it by rescaling df=0 images.
        parameters = {'dx':dx,'dy':dy,'ds':ds,'dg1':dg1,'dg2':dg2}
        if parameters != self.last_parameters:
            # We always render and cache using the nominal flux df=0.
            model = self.galaxy.get_transformed_model(**parameters)
            convolved = galsim.Convolve([
                model.shift(dx = -self.dx_arcsec,dy = -self.dy_arcsec),
                self.psf_model
                ],gsparams = self.gsparams)
            convolved.drawImage(image = self.stamp)
            self.stamp.array[self.mask] = 0.
            self.last_parameters = parameters
        # Return a copy of our cached image, applying flux rescaling if necessary.
        return (1.+df)*self.stamp

class Engine(object):
    """Rendering engine to simulate survey observations.

    Any pixels outside of the truncation radius or below the minimum S/N cut will have their
    flux set to zero in the rendered image. As a result the total rendered flux may be below
    the total model flux.

    Args:
        survey(descwl.survey.Survey): Survey that rendered images will simulate.
        min_snr(float): Simulate signals from individual sources down to this S/N threshold,
            where the signal N is calculated for the full exposure time and the noise N is
            set by the expected fluctuations in the sky background during a full exposure.
        truncate_radius(float): All extended sources are truncated at this radius in arcseconds.
        no_margin(bool): Do not simulate the tails of objects just outside the field.
        verbose_render(bool): Provide verbose output on rendering process.
    """
    def __init__(self,survey,min_snr,truncate_radius,no_margin,verbose_render):
        self.survey = survey
        self.min_snr = min_snr
        self.truncate_radius = truncate_radius
        self.no_margin = no_margin
        self.verbose_render = verbose_render
        # Calculate pixel flux threshold in electrons per pixel that determines how big a
        # bounding box we simulate for each source.
        sky_noise = math.sqrt(survey.mean_sky_level)
        self.pixel_cut = self.min_snr*sky_noise
        # Initialize our GalSim parameters.
        self.galsim_params = galsim.GSParams(maximum_fft_size=1<<16)
        # Evaluate the PSF dilution factor as the maximum fraction of a source's total flux
        # that can end up in a single pixel after convolution with the PSF.
        psf_stamp = galsim.ImageD(1,1,scale=self.survey.pixel_scale)
        self.survey.psf_model.drawImage(image = psf_stamp)
        self.psf_dilution = psf_stamp.array[0]
        # We will render each source into a square stamp with width = height = 2*padding + 1.
        self.padding = int(math.ceil(self.truncate_radius/self.survey.pixel_scale - 0.5))
        size = 2*self.padding + 1
        self.stamp = galsim.Image(size,size,scale = self.survey.pixel_scale, dtype = np.float32)
        # Prepare a truncation mask.
        pixel_grid = np.arange(-self.padding,self.padding+1)*self.survey.pixel_scale
        pixel_x,pixel_y = np.meshgrid(pixel_grid,pixel_grid)
        pixel_radius = np.sqrt(pixel_x**2 + pixel_y**2)
        self.truncation_mask = (pixel_radius <= self.truncate_radius)

    def description(self):
        """Describe our rendering configuration.

        Returns:
            str: Description of the rendering configuration that we be used to simulate
                the survey.
        """
        return '\n'.join([
            ('Will render all pixels with at least %.1f detected electrons.' % self.pixel_cut),
            ('PSF dilution factor is %.6f.' % self.psf_dilution)
            ])

    def render_galaxy(self,galaxy,no_partials = False):
        """Render a galaxy model for a simulated survey.

        Args:
            galaxy(descwl.model.Galaxy): Model of the galaxy to render.
            no_partials(bool): Do not calculate partial derivative images.

        Returns:
            tuple: `(stamps,bounds)` where `stamps` is a :class:`numpy.ndarray` of shape
                (nstamp,width,height) pixel values that represents nstamp postage-stamp images
                with the same dimensions (width,height) determined by the rendering options
                provided. The returned `bounds` give the position of these stamps within the
                full simulated survey image as a `galsim.BoundsI` object. Note that these bounds
                might extend beyond the survey image, but will always have some overlap where
                the source is above threshold.

        Raises:
            SourceNotVisible: Galaxy has no pixels above threshold that are visible in the
                simulated survey.
        """
        # Skip sources that are too faint to possibly be above our cut after PSF convolution.
        if galaxy.model.getFlux()*self.psf_dilution < self.pixel_cut:
            raise SourceNotVisible

        # Calculate the offset of the source center from the bottom-left corner of the
        # simulated image in floating-point pixel units.
        centroid = galaxy.model.centroid()
        x_center_pixels,y_center_pixels = self.survey.get_image_coordinates(centroid.x,centroid.y)

        # Calculate the corresponding central pixel indices in the full image, where (0,0) is the
        # bottom-left corner.
        x_center_index = int(math.floor(x_center_pixels))
        y_center_index = int(math.floor(y_center_pixels))

        # Calculate the bounding box to use for simulating this galaxy.
        x_min = x_center_index - self.padding
        x_max = x_center_index + self.padding
        y_min = y_center_index - self.padding
        y_max = y_center_index + self.padding

        # Calculate the offset of the bounding box center from the image center in arcsecs.
        dx_stamp_arcsec = 0.5*(x_min + x_max+1 - self.survey.image_width)*self.survey.pixel_scale
        dy_stamp_arcsec = 0.5*(y_min + y_max+1 - self.survey.image_height)*self.survey.pixel_scale

        # Shift the model to the bounding box center and convolve with the survey PSF.
        # We do not convolve by the pixel response since drawImage takes care of this.
        model = galsim.Convolve([
            galaxy.model.shift(dx=-dx_stamp_arcsec,dy=-dy_stamp_arcsec),
            self.survey.psf_model
            ],gsparams=self.galsim_params)

        # Render the model in our postage stamp.
        self.stamp.setOrigin(x_min,y_min)
        model.drawImage(image = self.stamp, use_true_center = True)

        # Identify pixels with flux above our cut and within our truncation radius
        # and zero all other pixel fluxes.
        keep_mask = (self.stamp.array*self.truncation_mask > self.pixel_cut)
        if np.sum(keep_mask) == 0:
            raise SourceNotVisible
        self.stamp.array[np.logical_not(keep_mask)] = 0.

        # Crop the bounding box.
        x_projection = (np.sum(keep_mask,axis=0) > 0)
        y_projection = (np.sum(keep_mask,axis=1) > 0)
        x_min_inset = np.argmax(x_projection)
        x_max_inset = np.argmax(x_projection[::-1])
        y_min_inset = np.argmax(y_projection)
        y_max_inset = np.argmax(y_projection[::-1])
        cropped_bounds = galsim.BoundsI(
            x_min+x_min_inset,x_max-x_max_inset,
            y_min+y_min_inset,y_max-y_max_inset)
        cropped_stamp = self.stamp[cropped_bounds]

        # Add the rendered model to the survey image.
        survey_overlap = cropped_bounds & self.survey.image.bounds
        if survey_overlap.area() == 0:
            raise SourceNotVisible
        self.survey.image[survey_overlap] += cropped_stamp[survey_overlap]

        # Give this Galaxy its own GalaxyRenderer.
        galaxy.renderer = GalaxyRenderer(galaxy,cropped_stamp,self.survey)

        # Define the parameter variations we consider for building Fisher matrices.
        # The names appearing below are args of Galaxy.get_transformed_model().
        # We do not include 'flux' below since the nominal image is already the
        # partial derivative wrt flux (after dividing by flux).
        variations = [
            ('dx',self.survey.pixel_scale/3.), # arcsecs
            ('dy',self.survey.pixel_scale/3.), # arcsecs
            ('ds',0.05), # relative dilation (flux preserving)
            ('dg1',0.03), # + shear using |g| = (a-b)/(a+b) convention
            ('dg2',0.03), # x shear using |g| = (a-b)/(a+b) convention
            ]

        # Prepare the datacube that we will return.
        if no_partials:
            ncube = 1
        else:
            # The nominal image doubles as the flux partial derivative.
            ncube = 1+len(variations)
        height,width = cropped_stamp.array.shape
        datacube = np.empty((ncube,height,width))
        datacube[0] = cropped_stamp.array

        # Calculate partial derivative images, if requested.
        if not no_partials:
            for i,(pname,delta) in enumerate(variations):
                variation_stamp = (galaxy.renderer.draw(**{pname: +delta}).copy() - 
                    galaxy.renderer.draw(**{pname: -delta}))
                datacube[i+1] = variation_stamp.array/(2*delta)

        if self.verbose_render:
            print 'Rendered galaxy model for id = %d with z = %.3f' % (
                galaxy.identifier,galaxy.redshift)
            print 'bounds: [%d:%d,%d:%d] w,h = %d,%d' % (
                x_min,x_max,y_min,y_max,x_max-x_min+1,y_max-y_min+1)
            print ' shift: (%.6f,%.6f) arcsec relative to stamp center' % (
                model.centroid().x,model.centroid().y)

        return datacube,cropped_bounds

    @staticmethod
    def add_args(parser):
        """Add command-line arguments for constructing a new :class:`Engine`.

        The added arguments are our constructor parameters with '_' replaced by '-' in the names.
        Note that constructor parameter defaults are specified here rather than in the constructor,
        so that they are included in command-line help.

        Args:
            parser(argparse.ArgumentParser): Arguments will be added to this parser object using its
                add_argument method.
        """
        parser.add_argument('--min-snr', type = float, default = 0.05, metavar = 'SNR',
            help = 'Simulate signals from individual sources down to this S/N threshold.')
        parser.add_argument('--truncate-radius', type = float, default = 30., metavar = 'SIZE',
            help = 'All extended sources are truncated at this radius in arcseconds.')
        parser.add_argument('--no-margin', action = 'store_true',
            help = 'Do not simulate the tails of objects just outside the field.')
        parser.add_argument('--verbose-render', action = 'store_true',
            help = 'Provide verbose output on rendering process.')

    @classmethod
    def from_args(cls,survey,args):
        """Create a new :class:`Engine` object from a set of arguments.

        Args:
            survey(descwl.survey.Survey): Survey that rendered images will simulate.
            args(object): A set of arguments accessed as a :py:class:`dict` using the
                built-in :py:func:`vars` function. Any extra arguments beyond those defined
                in :func:`add_args` will be silently ignored.

        Returns:
            :class:`Engine`: A newly constructed Engine object.
        """
        # Look up the named constructor parameters.
        pnames = (inspect.getargspec(cls.__init__)).args[1:]
        # Get a dictionary of the arguments provided.
        args_dict = vars(args)
        # Filter the dictionary to only include constructor parameters.
        filtered_dict = { key:args_dict[key] for key in (set(pnames) & set(args_dict)) }
        return cls(survey,**filtered_dict)
