'''
    The collections module contains information and logic related to the
    data users want to analyze using a Plant IT pipeline.

    Definitions:
        **sample**: The individual unit fed into a Plant IT pipeline. What is
            contained in a sample is dependent on the pipeline that will analyze
            the sample. For example, a sample may be an individual image, a
            folder containing slices from 3D imaging, or a csv file contining
            points for a cloud map.

            Plant IT makes no assumptions about what a sample is. Sample format
            is defined by the pipeline that will analyze it.

        **collection**: A set of samples related in some way that will be
            analyzed by the *same* pipeline.

            For example, all the roots collected in an experiment may go into
            one collection, as they will all be analyzed by the same
            downstream pipeline.
'''
