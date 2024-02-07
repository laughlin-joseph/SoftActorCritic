import numpy as np
import torch
import torch.nn as nn
from torch.distributions.normal import Normal

def combined_shape(length, shape=None):
    if shape is None:
        return (length,)
    return (length, shape) if np.isscalar(shape) else (length, *shape)

def mlp(sizes, activation, output_activation=nn.Identity):
    layers = []
    for j in range(len(sizes)-1):
        act = activation if j < len(sizes)-2 else output_activation
        layers += [nn.Linear(sizes[j], sizes[j+1]), act()]
    return nn.Sequential(*layers)

def count_vars(module):
    return sum([np.prod(p.shape) for p in module.parameters()])

def freeze_thaw_parameters(module, freeze=True):
    if freeze:
        for p in module.parameters():
            p.requires_grad = False
    else:
        for p in module.parameters():
            p.requires_grad = True
            
class SACReplayBuffer:

    def __init__(self, obs_dim, act_dim, size):
        self.obs_buf = np.zeros(combined_shape(size, obs_dim), dtype=np.float32)
        self.obs2_buf = np.zeros(combined_shape(size, obs_dim), dtype=np.float32)
        self.act_buf = np.zeros(combined_shape(size, act_dim), dtype=np.float32)
        self.rew_buf = np.zeros(size, dtype=np.float32)
        self.done_buf = np.zeros(size, dtype=np.float32)
        self.ptr, self.size, self.max_size = 0, 0, size

    def store(self, obs, act, rew, next_obs, done):
        self.obs_buf[self.ptr] = obs
        self.obs2_buf[self.ptr] = next_obs
        self.act_buf[self.ptr] = act
        self.rew_buf[self.ptr] = rew
        self.done_buf[self.ptr] = done
        self.ptr = (self.ptr+1) % self.max_size
        self.size = min(self.size+1, self.max_size)

    def sample_batch(self, batch_size=32):
        indexes = np.random.randint(0, self.size, size=batch_size)
        batch = dict(obs=self.obs_buf[indexes],
                     obs2=self.obs2_buf[indexes],
                     act=self.act_buf[indexes],
                     rew=self.rew_buf[indexes],
                     done=self.done_buf[indexes])
        return {k: torch.as_tensor(v, dtype=torch.float32) for k,v in batch.items()}

class SquashedGaussianMLPActor(nn.Module):
    
    def __init__(self, obs_dim, act_dim, hidden_sizes, activation, act_range):
        super().__init__()
        self.LOG_STD_MAX = 2
        self.LOG_STD_MIN = -20
        self.net = mlp([obs_dim] + list(hidden_sizes), activation, activation)
        self.mu_layer = nn.Linear(hidden_sizes[-1], act_dim)
        self.log_std_layer = nn.Linear(hidden_sizes[-1], act_dim)
        self.act_min, self.act_max = act_range[0], act_range[1]

    def forward(self, obs, deterministic=False, with_logprob=True):
        net_out = self.net(obs)
        mu = self.mu_layer(net_out)
        log_std = self.log_std_layer(net_out)
        log_std = torch.clamp(log_std, self.LOG_STD_MIN, self.LOG_STD_MAX)
        std = torch.exp(log_std)

        #Pre-squash distribution and sample
        pi_distribution = Normal(mu, std)
        if deterministic:
            #Average returned when testing
            pi_action = mu
        else:
            pi_action = pi_distribution.rsample()

        if with_logprob:
            # Compute logprob from Gaussian, and then apply correction for Tanh squashing.
            # NOTE: The correction formula is a little bit magic. To get an understanding 
            # of where it comes from, check out the original SAC paper (arXiv 1801.01290) 
            # and look in appendix C. This is a more numerically-stable equivalent to Eq 21.
            # Try deriving it yourself as a (very difficult) exercise. :)
            logprob_pi = pi_distribution.log_prob(pi_action).sum(axis=-1)
            logprob_pi -= (2*(np.log(2) - pi_action - nn.functional.softplus(-2*pi_action))).sum(axis=1)
        else:
            logprob_pi = None


        #rmin = Min of range of measure
        #rmax = Max of range of measure
        #tmin = Min of target range
        #tmax = Max of target range
        #measurement is in [rmin,rmax], the measured value to be scaled
        #tanh has a range of -1 to 1
        #((measurement - rmin)/(rmax - rmin)) * (tmax - tmin) + tmin
        
        squish_min, squish_max = -1, 1
        pi_action = torch.tanh(pi_action)
        #Scale to action range.
        pi_action = ((pi_action - squish_min)/(squish_max - squish_min)) * (self.act_max - self.act_min) + self.act_min
                     
        return pi_action, logprob_pi


class MLPQFunction(nn.Module):

    def __init__(self, obs_dim, act_dim, hidden_sizes, activation):
        super().__init__()
        
        #Cat obs and act dims for input layer, add hidden layers, output Q.
        self.q = mlp([obs_dim + act_dim] + list(hidden_sizes) + [1], activation)

    def forward(self, obs, act):
        q = self.q(torch.cat([obs, act], dim=-1))
        #Reshape val returned from Q network MLP.
        return torch.squeeze(q, -1)

class MLPActorCritic(nn.Module):

    def __init__(self, observation_space, action_space, hidden_sizes=(256,256), activation=nn.ReLU):
        super().__init__()

        obs_dim = observation_space['observation'].shape[0]
        act_dim = action_space.shape[0]
        act_range = (action_space.low[0], action_space.high[0])

        #Build actor, critic1, critic2, targ1, targ2 networksS
        self.pi = SquashedGaussianMLPActor(obs_dim, act_dim, hidden_sizes, activation, act_range)
        self.q1 = MLPQFunction(obs_dim, act_dim, hidden_sizes, activation)
        self.q2 = MLPQFunction(obs_dim, act_dim, hidden_sizes, activation)
        self.q1targ = MLPQFunction(obs_dim, act_dim, hidden_sizes, activation)
        self.q2targ = MLPQFunction(obs_dim, act_dim, hidden_sizes, activation)
        
        #Freeze target networks, these are updated with a Polyak average.
        freeze_thaw_parameters(self.q1targ)
        freeze_thaw_parameters(self.q2targ)

    def act(self, obs, deterministic=False):
        with torch.no_grad():
            a, _ = self.pi(obs, deterministic, False)
            return a.numpy()