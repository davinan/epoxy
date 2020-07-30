import numpy as np
import faiss
from sklearn.metrics import pairwise
import cytoolz as tz
import torch

class Epoxy:
    '''
    Class wrapping the functionality to extend LF's.
    
    Uses FAISS for nearest-neighbor search under the hood.
    '''
    
    def __init__(
        self,
        L_train,
        train_embeddings,
        gpu = False,
        metric = 'cosine',
        method = 'pytorch'
    ):
        '''
        Initialize an instance of Epoxy.
        
        Args:
            L_train: The training matrix to look through to find nearest neighbors
            train_embeddings: embeddings of each item in L_train
            gpu: if True, build the FAISS index on GPU
            metric: 'cosine' or 'L2' -- prefer cosine
            method: 'pytorch', 'faiss', or 'sklearn'
        '''
        self.L_train = L_train
        self.gpu = gpu
        self.metric = metric
        self.method = method
        
        if self.method == 'faiss':
            if metric == 'cosine':
                # Copy because faiss.normalize_L2() modifies the original
                train_embeddings = np.copy(train_embeddings)

                # Normalize the vectors before adding to the index
                faiss.normalize_L2(train_embeddings)
            elif metric == 'L2':
                pass
            else:
                raise NotImplementedError('Metric {} not supported'.format(metric))

            d = train_embeddings.shape[1]
            m = L_train.shape[1]

            self.m = m

            if metric == 'cosine':
                # use IndexFlatIP (inner product)
                label_fn_indexes = [faiss.IndexFlatIP(d) for i in range(m)]  
            else:  # 'L2':
                label_fn_indexes = [faiss.IndexFlatL2(d) for i in range(m)]

            if gpu:
                res = faiss.StandardGpuResources()
                label_fn_indexes = [faiss.index_cpu_to_gpu(res, 0, x) for x in label_fn_indexes]

            support = []
            for i in range(m):
                support.append(np.argwhere(L_train[:, i] != 0).flatten())
                label_fn_indexes[i].add(train_embeddings[support[i]])

            self.label_fn_indexes = label_fn_indexes
            self.support = support
        elif self.method in ['sklearn', 'pytorch']:
            if self.metric != 'cosine':
                raise NotImplementedError('Metric {} not supported for sklearn'.format(self.metric))
            self.train_embeddings = train_embeddings
        else:
            raise NotImplementedError('Method {} not supported'.format(self.method))
    
    def preprocess(
        self,
        L_mat,
        mat_embeddings,
        batch_size = None
    ):
        '''
        Preprocess L_mat for extension.
        
         Args:
            L_mat: The matrix to extend
            mat_embeddings: embeddings of each item in L_mat
            batch_size: if not None, compute in batches of this size
                (especially important for PyTorch similarity if done on GPU)
        '''
        self.L_mat = L_mat
        
        if self.method == 'faiss':
            mat_abstains = [
                np.argwhere(L_mat[:, i] == 0).flatten()
                for i in range(self.m)
            ]

            self.mat_abstains = mat_abstains

            dists_and_nearest = []
            for i in range(self.m):
                if self.metric == 'cosine':
                    embs_query = np.copy(mat_embeddings[mat_abstains[i]])
                    faiss.normalize_L2(embs_query)
                else:
                    embs_query = mat_embeddings[mat_abstains[i]]
                if batch_size is not None:
                    raise NotImplementedError('batch_size not supported yet for FAISS')
                dists_and_nearest.append(self.label_fn_indexes[i].search(embs_query, 1))

            self.dists = [
                dist_and_nearest[0].flatten()
                for dist_and_nearest in dists_and_nearest
            ]
            self.nearest = [
                dist_and_nearest[1].flatten()
                for dist_and_nearest in dists_and_nearest
            ]
        elif self.method in ['sklearn', 'pytorch']:
            self.mat_embeddings = mat_embeddings
            
            def comp_similarity(embs):
                if self.method == 'sklearn':
                    return pairwise.cosine_similarity(embs, self.train_embeddings)
                else:
                    if self.gpu:
                        return pytorch_cosine_similarity(
                            torch.tensor(embs).type(torch.float16).cuda(),
                            torch.tensor(self.train_embeddings).type(torch.float16).cuda()
                        ).cpu().numpy()
                    else:
                        return pytorch_cosine_similarity(
                            torch.tensor(embs).type(torch.float32),
                            torch.tensor(self.train_embeddings).type(torch.float32)
                        ).numpy()
            
            if batch_size is None:
                mat_to_train_sims = comp_similarity(self.mat_embeddings)
            else:
                sims = []
                for mat_batch in tz.partition_all(batch_size, self.mat_embeddings):
                    sims.append(comp_similarity(mat_batch))
                
                mat_to_train_sims = np.concatenate(sims)
                
            self.mat_to_train_sims = mat_to_train_sims
            
            mat_abstains, closest_pos, closest_neg = preprocess_lfs(
                self.L_train, self.L_mat, mat_to_train_sims
            )

            self.mat_abstains = mat_abstains
            self.closest_pos = closest_pos
            self.closest_neg = closest_neg
        
    def extend(self, thresholds):
        '''
        Extend L_mat (which was specified during pre-processing).
        '''
        if self.method == 'faiss':
            expanded_L_mat = np.copy(self.L_mat)

            new_points = [
                self.dists[i] > thresholds[i]
                for i in range(self.m)
            ]

            for i in range(self.m):
                expanded_L_mat[
                    self.mat_abstains[i][new_points[i]], i
                ] = self.L_train[
                    self.support[i], i
                ][self.nearest[i][new_points[i]]]

            return expanded_L_mat
        elif self.method in ['sklearn', 'pytorch']:
            return extend_lfs(
                self.L_mat, self.mat_abstains, self.closest_pos, self.closest_neg,
                thresholds
            )

def pytorch_cosine_similarity(a, b):
    """
    https://stackoverflow.com/questions/50411191/how-to-compute-the-cosine-similarity-in-pytorch-for-all-rows-in-a-matrix-with-re
    """
    a_norm = a / a.norm(dim=1)[:, None]
    b_norm = b / b.norm(dim=1)[:, None]
    return torch.mm(a_norm, b_norm.transpose(0,1))
        
def preprocess_lfs(
    L_train,
    L_mat,
    sim_from_mat_to_train
):
    '''
    Preprocessing for sklearn method.
    
    Preprocess similarity scores and get the closest item in the support set for
    each LF.
    
    Args:
        L_train: The training matrix to look through to find nearest neighbors
        L_mat: The matrix to extend
        sim_from_mat_to_train: Similarity scores from L_mat to L_train.
            sim_from_mat_to_train[i][j] stores the similarity between element i of
            L_mat to element j of L_train.
            
    Returns:
        A tuple of three Numpy matrices.
        The first matrix stores which elements of L_mat have abstains,
        the second matrix stores, for each labeling function, the closest point in
        L_train where that same labeling function voted positive, and the third
        matrix stores, for each labeling function, the closest point in L_train
        where the labeling function voted negative.
    '''
    m = L_mat.shape[1]
    expanded_L_mat = np.copy(L_mat)

    train_support_pos = [
        np.argwhere(L_train[:, i] == 1).flatten()
        for i in range(m)
    ]
    train_support_neg = [
        np.argwhere(L_train[:, i] == -1).flatten()
        for i in range(m)
    ]

    mat_abstains = [
        np.argwhere(L_mat[:, i] == 0).flatten()
        for i in range(m)
    ]

    pos_dists = [
        sim_from_mat_to_train[mat_abstains[i]][:, train_support_pos[i]]
        for i in range(m)
    ]
    neg_dists = [
        sim_from_mat_to_train[mat_abstains[i]][:, train_support_neg[i]]
        for i in range(m)
    ]

    closest_pos = [
        np.max(pos_dists[i], axis=1)
        if pos_dists[i].shape[1] > 0 else np.full(mat_abstains[i].shape, -1)
        for i in range(m)
    ]
    closest_neg = [
        np.max(neg_dists[i], axis=1)
        if neg_dists[i].shape[1] > 0 else np.full(mat_abstains[i].shape, -1)
        for i in range(m)
    ]

    return mat_abstains, closest_pos, closest_neg

def extend_lfs(
    L_mat,
    mat_abstains,
    closest_pos,
    closest_neg,
    thresholds
):
    '''
    Preprocessing for sklearn method.
    
    Extend LF's with fixed thresholds.
    
    Args:
        L_mat: The matrix to extend.
        mat_abstains, closest_pos, closest_neg: The outputs of the preprocess_lfs
            function.
        thresholds: The thresholds to extend each LF. For each item that an LF
            abstains on, if closest point that the LF votes on in the training
            is closer to the threshold, the LF is extended with the vote on that
            point. This information is encoded in mat_abstains, closest_pos, and
            closest_neg.
    
    Returns:
        An extended version of L_mat.
    '''
    m = L_mat.shape[1]
    expanded_L_mat = np.copy(L_mat)
    
    new_pos = [
        (closest_pos[i] > closest_neg[i]) & (closest_pos[i] > thresholds[i])
        for i in range(m)
    ]
    new_neg = [
        (closest_neg[i] > closest_pos[i]) & (closest_neg[i] > thresholds[i])
        for i in range(m)
    ]

    for i in range(m):
        expanded_L_mat[mat_abstains[i][new_pos[i]], i] = 1
        expanded_L_mat[mat_abstains[i][new_neg[i]], i] = -1
    
    return expanded_L_mat
