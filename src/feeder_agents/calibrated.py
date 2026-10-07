"""Sample conditional empirical marginals; coordinate angles remain a prior."""
import math
import random

from .artifacts import digest
from .calibration import load_profile
from .schemas import Bus, Line
from .settlements import size_conductors


def _offspring_sequence(values, count, rng):
    """Finite-size conditioning: sum offspring=count-1 and connected BFS order."""
    maximum = max(int(v) for v in values)
    if maximum < 1:
        raise ValueError('Calibration offspring distribution cannot produce a tree')
    offspring = [min(count-1,int(rng.choice(values))) for _ in range(count)]
    changes = 0
    delta = count-1-sum(offspring)
    while delta:
        eligible = [i for i,v in enumerate(offspring) if v < min(maximum,count-1)] if delta>0 else [i for i,v in enumerate(offspring) if v>0]
        index = rng.choice(eligible)
        step = 1 if delta>0 else -1
        offspring[index] += step
        delta -= step
        changes += 1
    # Cycle lemma: rotate after the minimum cumulative offspring-minus-one.
    running = minimum = start = 0
    for i,value in enumerate(offspring):
        running += value-1
        if running < minimum:
            minimum,start = running,i+1
    start %= count
    return offspring[start:]+offspring[:start],changes


def design_empirical(feeder,spec):
    profile = load_profile(spec.scenario.calibration_profile)
    distributions = profile['distributions']
    lengths = distributions['mv_segment_length_km']['values']
    eligible = [float(x) for x in lengths if spec.segment_km_min <= x <= spec.segment_km_max]
    if not eligible:
        raise ValueError('No observed MV segment lengths within requested bounds; calibration was not silently extrapolated')
    rng = random.Random(f'empirical:{feeder.seed}')
    mixture = spec.scenario.empirical_weight
    # Explicit weak structural prior P(children=0,1,2,3)=(.30,.45,.20,.05).
    # Its expectation is one; finite-tree conditioning is still necessary.
    observed_offspring = distributions['offspring_count']['values']
    offspring_pool = [int(rng.choice(observed_offspring)) if rng.random()<mixture else
        rng.choices([0,1,2,3],weights=[.30,.45,.20,.05])[0] for _ in range(max(200,len(feeder.buses)*4))]
    offspring,changes = _offspring_sequence(offspring_pool,len(feeder.buses),rng)
    buses = [Bus(id='source',x_km=0,y_km=0)]
    lines = []
    angles = [0.]
    # Sibling directions are separated; angle dispersion is an explicit
    # engineering prior, not estimated from geographic information.
    for parent,children in enumerate(offspring):
        for child in range(children):
            index = len(buses)
            angle = angles[parent]+(child-(children-1)/2)*.65+rng.uniform(-.12,.12)
            length = rng.choice(eligible) if rng.random()<mixture else rng.uniform(spec.segment_km_min,spec.segment_km_max)
            bus = Bus(id=f'b{index}',x_km=buses[parent].x_km+length*math.cos(angle),
                y_km=buses[parent].y_km+length*math.sin(angle))
            buses.append(bus);angles.append(angle)
            lines.append(Line(id=f'l{index}',bus1=buses[parent].id,bus2=bus.id,
                length_km=length,conductor='research_small'))
    feeder.buses,feeder.lines = buses,lines
    weights = [float(rng.choice(distributions['load_weight']['values'])) if rng.random()<mixture else
        rng.lognormvariate(-.125,.5) for _ in feeder.loads]
    weight_total = sum(weights)
    q_over_p = math.tan(math.acos(spec.power_factor))
    for load,weight in zip(feeder.loads,weights):
        load.kw = feeder.total_kw*weight/weight_total
        load.kvar = load.kw*q_over_p
        load.pv_kw = load.kw*spec.pv_ratio
        load.contract_kva = load.kw/spec.power_factor
    feeder.design_evidence = {
        'method':'empirical_tree_v1','profile_id':spec.scenario.calibration_profile,
        'profile_hash':digest(profile),'status':'partially_calibrated_marginals',
        'empirical_weight':mixture,'weight_status':'explicit_transfer_assumption_not_fitted_confidence',
        'empirical_fields':['mv_segment_length_km','offspring_count','relative_positive_load_weight'],
        'length_observations_total':len(lengths),'length_observations_in_bounds':len(eligible),
        'length_retained_fraction':len(eligible)/len(lengths),
        'offspring_conditioning_adjustments':changes,
        'unobserved_priors':{'sibling_angle_separation_rad':.65,'angle_jitter_rad':.12,
            'equipment':'illustrative catalogue sized at 80% ampacity','load_placement':'one aggregate load per non-source bus'},
        'mixture_priors':{'length':'uniform within user bounds','offspring_probabilities':[.30,.45,.20,.05],
            'offspring_support':[0,1,2,3],'relative_load':'lognormal(mu=-0.125,sigma=0.5)'},
        'initial_conductor_sizing':size_conductors(feeder),
    }
    feeder.assumptions = [a for a in feeder.assumptions if not a.startswith('No statistical calibration')]
    feeder.assumptions += [
        'Partial real-feeder marginal calibration: MV segment length, offspring counts and normalized positive demand weights only.',
        'Reference lengths are conditioned on user bounds; offspring degrees conditioned on fixed node count; load weights renormalized to requested total.',
        'US feeder reference distributions transferred to balanced 10kV research cases; no Chinese rural/urban population representativeness claim.',
        'Angles, source strength, equipment catalogue, load attachment and PV placement remain research priors; no joint or geographic calibration.',
        'Empirical line lengths are modeled as straight segments; real conductor route geometry is not reproduced.',
    ]
    return feeder
