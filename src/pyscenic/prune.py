# -*- coding: utf-8 -*-

import pandas as pd
import re
from .utils import load_motif_annotations
from .rnkdb import RankingDatabase, MemoryDecorator
from operator import concat
from boltons.iterutils import chunked_iter
from dask.multiprocessing import get
from dask import delayed
from dask.distributed import LocalCluster, Client
from typing import Type, Sequence, Union
from .genesig import Regulome, GeneSignature
from math import ceil
from functools import partial
# Using multiprocessing using dill package for pickling to avoid strange bugs.
from multiprocessing import cpu_count
from multiprocessing_on_dill.connection import Pipe
from multiprocessing_on_dill.context import Process
import datetime
from .utils import add_motif_url
from .algo import module2features_numba_impl, modules2regulomes, modules2df


# Taken from: https://www.regular-expressions.info/ip.html
IP_PATTERN = re.compile(r"""(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])\.(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])\.(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])\.(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])""")


def _prepare_client(client_or_address):
    """
    :param client_or_address: one of:
           * None
           * verbatim: 'local'
           * string address
           * a Client instance
    :return: a tuple: (Client instance, shutdown callback function).
    :raises: ValueError if no valid client input was provided.
    """
    # Credits to Thomas Moerman (arboretum package):
    # https://github.com/tmoerman/arboretum/blob/b065c6eade325ace104b2bb772ad15c78d573b1b/arboretum/algo.py#L139-L185

    if client_or_address is None or str(client_or_address).lower() == 'local':
        local_cluster = LocalCluster(diagnostics_port=None)
        client = Client(local_cluster)

        def close_client_and_local_cluster(verbose=False):
            if verbose:
                print('shutting down client and local cluster')

            client.close()
            local_cluster.close()

        return client, close_client_and_local_cluster

    elif isinstance(client_or_address, str) and client_or_address.lower() != 'local':
        client = Client(client_or_address)

        def close_client(verbose=False):
            if verbose:
                print('shutting down client')

            client.close()

        return client, close_client

    elif isinstance(client_or_address, Client):

        def close_dummy(verbose=False):
            if verbose:
                print('not shutting down client, client was created externally')

            return None

        return client_or_address, close_dummy

    else:
        raise ValueError("Invalid client specified {}".format(str(client_or_address)))


class Worker(Process):
    def __init__(self, name: str, db: Type[RankingDatabase], modules: Sequence[Regulome],
                 motif_annotations_fname: str, sender,
                 motif_similarity_fdr: float, orthologuous_identity_threshold: float,
                 transformation_func):
        super().__init__(name=name)
        self.database = db
        self.modules = modules
        self.motif_annotations_fname = motif_annotations_fname
        self.motif_similarity_fdr = motif_similarity_fdr
        self.orthologuous_identity_threshold = orthologuous_identity_threshold
        self.transform_fnc = transformation_func
        self.sender = sender

    def run(self):
        # Load ranking database in memory.
        rnkdb = MemoryDecorator(self.database)
        print("{} - Worker {}: database loaded in memory.".format(datetime.datetime.now(), self.name))

        # Load motif annotations in memory.
        motif_annotations = load_motif_annotations(self.motif_annotations_fname,
                                                   motif_similarity_fdr=self.motif_similarity_fdr,
                                                   orthologous_identity_threshold=self.orthologuous_identity_threshold)
        print("{} - Worker {}: motif annotations loaded in memory.".format(datetime.datetime.now(), self.name))

        # Apply transformation on all modules.
        output = self.transform_fnc(rnkdb, self.modules, motif_annotations=motif_annotations)
        print("{} - Worker {}: All regulomes derived.".format(datetime.datetime.now(), self.name))

        # Sending information back to parent process.
        # Another approach might be to write a CSV file (for dataframes) or YAML file (for regulomes) to a temp.
        # file and share the name of the file with the parent process.
        # Serialization introduces a performance penalty!
        self.sender.send(output)
        self.sender.close()
        print("{} - Worker {}: Done.".format(datetime.datetime.now(), self.name))


def _distributed_calc(rnkdbs: Sequence[Type[RankingDatabase]], modules: Sequence[Type[GeneSignature]],
                      motif_annotations_fname: str,
                      transform_func, aggregate_func,
                      motif_similarity_fdr: float = 0.001, orthologuous_identity_threshold: float = 0.0,
                      client_or_address='custom_multiprocessing',
                      num_workers=None, module_chunksize=100) -> Union[Sequence[Regulome], pd.DataFrame]:
    """
    Perform a parallelized or distributed calculation.

    :param rnkdbs: A sequence of ranking databases.
    :param modules: A sequence of gene signatures.
    :param motif_annotations_fname: The filename of the motif annotations to use.
    :param transform_func: A function having a signature (Type[RankingDatabase], Sequence[Type[GeneSignature]], str) and
        that returns Union[Sequence[Regulome]],pandas.DataFrame].
    :param aggregate_func: A function having a signature:
        - (Sequence[pandas.DataFrame]) => pandas.DataFrame
        - (Sequence[Sequence[Regulome]]) => Sequence[Regulome]
    :param motif_similarity_fdr: The maximum False Discovery Rate to find factor annotations for enriched motifs.
    :param orthologuous_identity_threshold: The minimum orthologuous identity to find factor annotations
        for enriched motifs.
    :param client_or_address: The client of IP address of the scheduler when working with dask. For local multi-core
        systems 'custom_multiprocessing' or 'dask_multiprocessing' can be supplied.
    :param num_workers: If not using a cluster, the number of workers to use for the calculation.
        None of all available CPUs need to be used.
    :param module_chunksize: The size of the chunk in signatures to use when using the dask framework.
    :return:
    """
    def is_valid(client_or_address):
        if isinstance(client_or_address, str) and ((client_or_address in
                                                    {"custom_multiprocessing", "dask_multiprocessing", "local"})
                                                   or IP_PATTERN.fullmatch(client_or_address)):
            return True
        elif isinstance(client_or_address, Client):
            return True
        return False
    assert is_valid(client_or_address), "\"{}\"is not valid for parameter client_or_address.".format(client_or_address)

    if client_or_address == 'custom_multiprocessing': # CUSTOM parallelized implementation.
        # This implementation overcomes the I/O-bounded performance. Each worker (subprocess) loads a dedicated ranking
        # database and motif annotation table into its own memory space before consuming module. The implementation of
        # each worker uses the AUC-first numba JIT based implementation of the algorithm.
        assert len(rnkdbs) <= num_workers if num_workers else cpu_count(), "The number of databases is larger than the number of cores."
        amplifier = int((num_workers if num_workers else cpu_count())/len(rnkdbs))
        print("Using {} workers.".format(len(rnkdbs) * amplifier))
        receivers = []
        for db in rnkdbs:
            for idx, chunk in enumerate(chunked_iter(modules, ceil(len(modules)/float(amplifier)))):
                sender, receiver = Pipe()
                receivers.append(receiver)
                Worker("{}({})".format(db.name, idx+1), db, chunk, motif_annotations_fname, sender,
                       motif_similarity_fdr, orthologuous_identity_threshold, transform_func).start()
        return aggregate_func([recv.recv() for recv in receivers])
    else: # DASK framework.
        # Load motif annotations.
        motif_annotations = load_motif_annotations(motif_annotations_fname,
                                                   motif_similarity_fdr=motif_similarity_fdr,
                                                   orthologous_identity_threshold=orthologuous_identity_threshold)

        # Create dask graph.
        def create_graph(client=None):
            # In a cluster the motif annotations need to be broadcasted to all nodes. Otherwise
            # the motif annotations need to wrapped in a delayed() construct to avoid needless pickling and
            # unpicking between processes.
            delayed_or_future_annotations = client.scatter(motif_annotations, broadcast=True) if client \
                                                else delayed(motif_annotations, pure=True)

            # Chunking the gene signatures might not be necessary anymore because the overhead of the dask
            # scheduler is minimal (cf. blog http://matthewrocklin.com/blog/work/2016/05/05/performant-task-scheduling).
            # The original behind the decision to implement this was the refuted assumption that fast executing tasks
            # would greatly be impacted by scheduler overhead. The chunking of signatures seemed to corroborate
            # this assumption. However, the benefit was through less pickling and unpickling of the motif annotations
            # dataframe as this was not wrapped in a delayed() construct.

            # Remark on sharing ranking databases across a cluster. Because the frontnodes of the VSC for the LCB share
            # a file server and have a common home folder configured, these database (stored on this shared drive)
            # can be accessed from all nodes in the cluster and can all use the same path in the configuration file.

            # TODO: A potential improvement to reduce I/O contention for this shared drive (accessing the ranking
            # TODO: database) would be to load the database in memory (using the available decorator) for each task.
            # TODO: The penalty of loading the database in memory should be shared across multiple gene signature so
            # TODO: in this case chunking of gene signatures is mandatory to avoid severe performance penalties.
            return delayed(aggregate_func)(
                        (delayed(transform_func)
                            (db, gs_chunk, delayed_or_future_annotations)
                                for db in rnkdbs
                                    for gs_chunk in chunked_iter(modules, module_chunksize)))

        # Compute dask graph ...
        if client_or_address == "dask_multiprocessing":
            # ... via multiprocessing.
            return create_graph().compute(get=get, num_workers=num_workers if num_workers else cpu_count())
        else:
            # ... via dask.distributed framework.
            client, shutdown_callback = _prepare_client(client_or_address)
            try:
                return client.compute(create_graph(client))
            finally:
                shutdown_callback(False)


def find_motifs(rnkdbs: Sequence[Type[RankingDatabase]], signatures: Sequence[Type[GeneSignature]],
                motif_annotations_fname: str,
                rank_threshold: int = 1500, auc_threshold: float = 0.05, nes_threshold=3.0,
                motif_similarity_fdr: float = 0.001, orthologuous_identity_threshold: float = 0.0,
                avgrcc_sample_frac: float = None,
                weighted_recovery=False, client_or_address='custom_multiprocessing',
                num_workers=None, module_chunksize=100,
                motif_base_url: str = "http://motifcollections.aertslab.org/v9/") -> pd.DataFrame:
    module2features_func = partial(module2features_numba_impl,
                                   rank_threshold=rank_threshold,
                                   auc_threshold=auc_threshold,
                                   nes_threshold=nes_threshold,
                                   avgrcc_sample_frac=avgrcc_sample_frac,
                                   filter_for_annotation=False)
    transformation_func = partial(modules2df, module2features_func=module2features_func, weighted_recovery=weighted_recovery)
    aggregation_func = pd.concat
    df = _distributed_calc(rnkdbs, signatures, motif_annotations_fname, transformation_func, aggregation_func,
                      motif_similarity_fdr, orthologuous_identity_threshold, client_or_address,
                      num_workers, module_chunksize)
    return add_motif_url(df, base_url=motif_base_url)


def prune_targets(rnkdbs: Sequence[Type[RankingDatabase]], modules: Sequence[Regulome],
                         motif_annotations_fname: str,
                         rank_threshold: int = 1500, auc_threshold: float = 0.05, nes_threshold=3.0,
                         motif_similarity_fdr: float = 0.001, orthologuous_identity_threshold: float = 0.0,
                         avgrcc_sample_frac: float = None,
                         weighted_recovery=False, client_or_address='custom_multiprocessing',
                         num_workers=None, module_chunksize=100, output="regulomes") -> Union[Sequence[Regulome], pd.DataFrame]:
    """
    Calculate all regulomes for a given sequence of ranking databases and a sequence of co-expression modules.

    :param rnkdbs: The sequence of databases.
    :param modules: The sequence of modules.
    :param motif_annotations_fname: The name of the file that contains the motif annotations to use.
    :param rank_threshold: The total number of ranked genes to take into account when creating a recovery curve.
    :param auc_threshold: The fraction of the ranked genome to take into account for the calculation of the
        Area Under the recovery Curve.
    :param nes_threshold: The Normalized Enrichment Score (NES) threshold to select enriched features.
    :param motif_similarity_fdr: The maximum False Discovery Rate to find factor annotations for enriched motifs.
    :param orthologuous_identity_threshold: The minimum orthologuous identity to find factor annotations
        for enriched motifs.
    :param avgrcc_sample_frac: The fraction of the features to use for the calculation of the average curve, If None
        then all features are used.
    :param weighted_recovery: Use weights of a gene signature when calculating recovery curves?
    :param num_workers: If not using a cluster, the number of workers to use for the calculation.
        None of all available CPUs need to be used.
    :param module_chunksize: The size of the chunk to use when using the dask framework.
    :param client_or_address: The client of IP address of the scheduler when working with dask. For local multi-core
        systems 'custom_multiprocessing' or 'dask_multiprocessing' can be supplied.
    :param output: The type of output requested, i.e. regulomes or a dataframe.
    :return: A sequence of regulomes.
    """
    assert output in {"regulomes", "df"}, "Invalid output type."

    module2features_func = partial(module2features_numba_impl,
                              rank_threshold=rank_threshold,
                              auc_threshold=auc_threshold,
                              nes_threshold=nes_threshold,
                              avgrcc_sample_frac=avgrcc_sample_frac,
                              filter_for_annotation=True)
    transformation_func = partial(modules2regulomes, module2features_func=module2features_func, weighted_recovery=weighted_recovery) \
        if output == "regulomes" else partial(modules2df, module2features_func=module2features_func, weighted_recovery=weighted_recovery)
    from toolz.curried import reduce
    aggregation_func = reduce(concat) if output == "regulomes" else pd.concat
    return _distributed_calc(rnkdbs, modules, motif_annotations_fname, transformation_func, aggregation_func,
                      motif_similarity_fdr, orthologuous_identity_threshold, client_or_address,
                      num_workers, module_chunksize)