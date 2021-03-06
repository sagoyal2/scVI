import torch
from torch.distributions import Normal, Multinomial, kl_divergence as kl

from scvi.models.base import SemiSupervisedModel
from scvi.models.classifier import Classifier, LinearLogRegClassifier
from scvi.models.modules import Decoder, Encoder
from scvi.models.utils import broadcast_labels
from scvi.models.vae import VAE


class SVAEC(VAE, SemiSupervisedModel):
    '''
    "Stacked" variational autoencoder for classification - SVAEC
    (from the stacked generative model M1 + M2)
    '''

    def __init__(self, n_input, n_batch, n_labels, n_hidden=128, n_latent=10, n_layers=1, dropout_rate=0.1,
                 y_prior=None, logreg_classifier=False, dispersion="gene", log_variational=True,
                 reconstruction_loss="zinb"):
        super(SVAEC, self).__init__(n_input, n_hidden=n_hidden, n_latent=n_latent, n_layers=n_layers,
                                    dropout_rate=dropout_rate, n_batch=n_batch, dispersion=dispersion,
                                    log_variational=log_variational, reconstruction_loss=reconstruction_loss)

        self.n_labels = n_labels
        self.n_latent_layers = 2
        # Classifier takes n_latent as input
        if logreg_classifier:
            self.classifier = LinearLogRegClassifier(n_latent, self.n_labels)
        else:
            self.classifier = Classifier(n_latent, n_hidden, self.n_labels, n_layers, dropout_rate)

        self.encoder_z2_z1 = Encoder(n_latent, n_latent, n_cat_list=[self.n_labels], n_layers=n_layers,
                                     n_hidden=n_hidden, dropout_rate=dropout_rate)
        self.decoder_z1_z2 = Decoder(n_latent, n_latent, n_cat_list=[self.n_labels], n_layers=n_layers,
                                     n_hidden=n_hidden, dropout_rate=dropout_rate)

        self.y_prior = torch.nn.Parameter(
            y_prior if y_prior is not None else (1 / self.n_labels) * torch.ones(self.n_labels), requires_grad=False
        )

    def classify(self, x):
        z = self.sample_from_posterior_z(x)
        return self.classifier(z)

    def get_latents(self, x, y=None):
        zs = super(SVAEC, self).get_latents(x)
        qz2_m, qz2_v, z2 = self.encoder_z2_z1(zs[0], y)
        if not self.training:
            z2 = qz2_m
        return [zs[0], z2]

    def forward(self, x, local_l_mean, local_l_var, batch_index=None, y=None):
        is_labelled = False if y is None else True

        x_ = torch.log(1 + x)
        qz1_m, qz1_v, z1 = self.z_encoder(x_)
        ql_m, ql_v, library = self.l_encoder(x_)

        # Enumerate choices of label
        ys, z1s = (
            broadcast_labels(
                y, z1, n_broadcast=self.n_labels
            )
        )
        qz2_m, qz2_v, z2 = self.encoder_z2_z1(z1s, ys)
        pz1_m, pz1_v = self.decoder_z1_z2(z2, ys)
        px_scale, px_r, px_rate, px_dropout = self.decoder(self.dispersion, z1, library, batch_index)

        reconst_loss = self._reconstruction_loss(x, px_rate, px_r, px_dropout, batch_index, y)

        # KL Divergence
        mean = torch.zeros_like(qz2_m)
        scale = torch.ones_like(qz2_v)

        kl_divergence_z2 = kl(Normal(qz2_m, torch.sqrt(qz2_v)), Normal(mean, scale)).sum(dim=1)
        loss_z1_unweight = - Normal(pz1_m, torch.sqrt(pz1_v)).log_prob(z1s).sum(dim=-1)
        loss_z1_weight = Normal(qz1_m, torch.sqrt(qz1_v)).log_prob(z1).sum(dim=-1)
        kl_divergence_l = kl(Normal(ql_m, torch.sqrt(ql_v)), Normal(local_l_mean, torch.sqrt(local_l_var))).sum(dim=1)

        if is_labelled:
            return reconst_loss + loss_z1_weight + loss_z1_unweight, kl_divergence_z2 + kl_divergence_l

        probs = self.classifier(z1)
        reconst_loss += (loss_z1_weight + ((loss_z1_unweight).view(self.n_labels, -1).t() * probs).sum(dim=1))

        kl_divergence = (kl_divergence_z2.view(self.n_labels, -1).t() * probs).sum(dim=1)
        kl_divergence += kl(Multinomial(probs=probs), Multinomial(probs=self.y_prior))

        return reconst_loss, kl_divergence
