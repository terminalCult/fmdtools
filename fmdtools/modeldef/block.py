# -*- coding: utf-8 -*-
"""
Description: A module to define Functions, Components, Actions, and other classes with behaviors.
    
- :class:`Block`:       Superclass for Functions, Components, Actions, etc.
- :class:`FxnBlock`:    Class for defining model Functions
- :class:`Component`:   Class for defining Components (which have behaviors and live in a function)
- :class:`Action`:      Class for defining Actions (which have behaviors and live in a function, but have updateact method)
- :class:`CompArch`:    Class for defining Component Architectures, or sets of components to be contained by a FxnBlock
- :class:`ASG`:         Class for defining Action Sequence Graphs, or sets of actions with specific relationships.
"""
import numpy as np
from decimal import Decimal
import sys
import itertools
import networkx as nx
import copy
import inspect
import warnings
from recordclass import dataobject, asdict

from .state import State
from .parameter import Parameter
from .rand import Rand
from .common import get_true_fields,get_true_field
from .time import Time
from .mode import Mode
from .flow import init_flow, Flow
from fmdtools.faultsim.result import History, get_sub_include, init_hist_iter

def assoc_flows(obj, flows={}):
    """
    Associates flows with with the given object (Block, ASG, etc.) 
    
    Flows must be defined with the _init_ class variable pointing to the class
    to initialize (e.g., _init_flowname = FlowClass).
    
    Parameters
    ----------
    obj: object (Block, ASG)
        block requiring the flows
    
    flows : dict, optional
        If flows is provided AND it contains a flowname corresponding to the
        function's flowname, it will be used instead (so that it can act as a 
        connection to the rest of the model)
    """
    if hasattr(obj, 'flownames'):
        flows = {obj.flownames.get(fn, fn): flow for fn, flow in flows.items()}
    for init_att in dir(obj):
        if init_att.startswith("_init_"):
            att = getattr(obj, init_att)
            attname = init_att[6:]
            if inspect.isclass(att) and issubclass(att, Flow) and not(attname in obj.flows):
                if attname in flows:    obj.flows[attname]=flows.pop(attname)
                else:                   obj.flows[attname]=att(attname)
                if not isinstance(obj, dataobject):
                    setattr(obj, attname, obj.flows[attname])
    if flows:
        warnings.warn("these flows sent from model "+str([*flows.keys()])+" not added to class "+str(obj.__class__))
def inject_faults_internal(obj, faults):
    """
    Injects faults in the CompArch/ASG object obj.

    Parameters
    ----------
    obj : TYPE
        DESCRIPTION.
    faults : TYPE
        DESCRIPTION.
    """
    if isinstance(obj, ASG):        compdict = obj.actions
    elif isinstance(obj, CompArch): compdict=obj.components
    else: raise Exception("Invalid object type: "+type(obj)+" should be ASG or CompArch") 

    for fault in faults:
        if fault in obj.faultmodes:
            comp = compdict[obj.faultmodes[fault]]
            comp.m.add_fault(fault[len(comp.name)+1:])


class Block():
    __slots__ = ['p', '_args_p', 's', '_args_s','m', '_args_m', 'r', '_args_r', 't', '_args_t', 'h', '__dict__']
    hist_atts = ['s', 'm', 'r', 't']
    _init_p = Parameter
    _init_s = State
    _init_m = Mode
    _init_r = Rand
    _init_t = Time
    name='block'
    flows = dict()
    is_copy = False
    """ 
    Superclass for FxnBlock and Component subclasses. Has functions for model setup, querying state, reseting the model
    
    Attributes
    ----------
    p : Parameter
        Internal Parameter for the block. Instanced from _init_p
    s : State
        Internal State of the block. Instanced from _init_s.
    m : Mode
        Internal Mode for the block. Instanced from _init_m
    r : Rand
        Internal Rand for the block. Instanced from _init_r
    t : Time
        Internal Time for the block. Instanced from _init_t
    name : str
        Block name
    flows : dict
        Dictionary of flows included in the Block (if any are added via _init_flowname)
    is_copy : bool
        Marker for whether the object is a copy.
    """
    def __init__(self, name, flows={}, s={}, p={}, m={}, r={}, t={}):
        """
        Instance superclass. Called by FxnBlock and Component classes.

        Parameters
        ----------
        name : str
            Name for the Block instance.
        flows :dict
            Flow objects passed from the model level to use instead of instantiating locally.
        p : dict, optional
            Internal parameters to override from defaults. The default is {}.
        s : dict, optional
            Internal states to override from defaults. The default is {}.
        c : dict, optional
            Internal CompArch fields/arguments override from defaults. The default is {}.
            FxnBlock must have an _init_c property.
        a : dict, optional
            Internal ASG fields/arguments override from defaults. The default is {}.
            FxnBlock must have an _init_a property.
        r : dict, optional
            Internal Rand fields/arguments override from defaults. The default is {}.
        r : dict, optional
            Internal Mode fields/arguments override from defaults. The default is {}.
        t : dict, optional
            Internal Time fields/arguments to override from defaults. The defautl is {}
        """
        self.name=name
        self.flows = dict()
        assoc_flows(self, flows=flows)
        for at in ['s','p','m','r', 't']:
            at_arg = eval(at)
            if type(at_arg)!=dict: at_arg = asdict(at_arg)
            setattr(self, '_args_'+at, at_arg)
            init_at = getattr(self, '_init_'+at)
            setattr(self, at, init_at(**at_arg))
        self.update_seed()
    def __repr__(self):
        if hasattr(self,'name'):
            return getattr(self, 'name', '')+' '+self.__class__.__name__+' '+getattr(self,'type', '')+': '+str(self.return_states())
        else: return 'New uninitialized '+self.__class__.__name__
    def update_seed(self, seed=[]):
        """
        Updates seed and propogates update to contained actions/components.
        (keeps seeds in sync)

        Parameters
        ----------
        seed : int, optional
            Random seed. The default is [].
        """
        if seed: self.r.seed=seed
        
        if hasattr(self, 'c'): self.c.update_seed(self.seed)
        if hasattr(self, 'a'): self.a.update_seed(self.seed)
    def get_rand_states(self, auto_update_only=False):
        """Gets dict of random states from block and associated actions/components"""
        rand_states = self.r.get_rand_states(auto_update_only)
        if hasattr(self, 'c'): rand_states.update(self.c.get_rand_states(auto_update_only=auto_update_only))
        if hasattr(self, 'actions'):
            for actname, act in self.actions.items():
                if act.get_rand_states(auto_update_only=auto_update_only): 
                    rand_states[actname] = act.get_rand_states(auto_update_only=auto_update_only)
        return rand_states
    def choose_rand_fault(self, faults, default='first', combinations=1):
        """
        Randomly chooses a fault or combination of faults to insert in fxn.m. 

        Parameters
        ----------
        faults : list
            list of fault modes to choose from
        default : str/list, optional
            Default fault to inject when model is run deterministically. 
            The default is 'first', which chooses the first in the list. 
            Can provide a mode as a str or a list of modes
        combinations : int, optional
            Number of combinations of faults to elaborate and select from. 
            The default is 1, which just chooses single fault modes.
        """
        if getattr(self.r, 'run_stochastic', True):
            faults = [list(x) for x in itertools.combinations(faults, combinations)]
            self.m.add_fault(*self.r.rng.choice(faults))
        elif default=='first':      self.m.add_fault(faults[0])
        elif type(default)==str:    self.m.add_fault(default)
        else:                       self.m.add_fault(*default)
    def get_flowtypes(self):
        """Returns the names of the flow types in the model"""
        return {obj.__class__.__name__ for name, obj in self.flows.items()}
    def reset(self):            #reset requires flows to be cleared first
        """ Resets the block to the initial state with no faults. Used by default in 
        derived objects when resetting the model. Requires associated flows to be cleared first."""
        self.m.remove_any_faults()
        self.s=self._init_s(**self._args_s)
        self.r=self._init_r(**self._args_r)
        self.t.reset()
        for flow in self.flows.values():
            flow.reset()
    def copy(self, flows={}, *args, **kwargs):
        cop = self.__new__(self.__class__)  # Is this adequate? Wouldn't this give it new components?
        cop.is_copy=True
        try:
            cop.__init__(self.name, flows, *args, **kwargs)
        except TypeError as e:
            raise Exception("Poor specification of "+str(self.__class__))  from e
        cop.m.mirror(self.m)
        cop.t=self.t.copy(**self._args_t)
        cop.s.assign(self.s)
        cop.r.assign(self.r)
        if hasattr(self, 'h'): cop.h =self.h.copy()
        return cop
    def get_memory(self):
        """ Gets the approximate memory usage of the block in bytes (not complete)"""
        mem=0
        mem+=sys.getsizeof(self.m.opermodes)
        if hasattr(self, 'r'):
            mem+=sys.getsizeof(self.r)
        if hasattr(self, 'm'):
            for fm in self.m.faultmodes.values():
                mem+=sys.getsizeof(fm)
        if hasattr(self, 'mode_state_dict'):
            mem+=sys.getsizeof(self.mode_state_dict)
        if hasattr(self, 'timers'):
            for timer in self.timers:
                mem+=sys.getsizeof(timer)
        if hasattr(self, 'internal_flows'):
            for flowname, flow in self.internal_flows.items():
                mem+= flow.get_memory()
        if hasattr(self, 'components'):
            for name,comp in self.c.components.items():
                mem+=comp.get_memory()
        if hasattr(self, 'actions'):
            for name,comp in self.actions.items():
                mem+=comp.get_memory()
        for state in asdict(self.s):
            mem+=2*sys.getsizeof(state) # (*2 because both the initstate and the actual state should be counted)
        return mem
    def return_states(self):
        """
        Returns states of the block at the current state. Used (iteratively) to record states over time.

        Returns
        -------
        states : dict
            States (variables) of the block
        faults : set
            Faults present in the block
        """
        states={}
        if hasattr(self, 's'):
            for state, val in asdict(self.s).items():
                if type(val) in [set, dict]: val=copy.deepcopy(val)
                states[state]= val
        states['mode']=self.m.mode
        return states, self.m.faults.copy()
    def has_new_states(self, prev_states, prev_faults):
        states, faults = self.return_states()
        if prev_states!=states or prev_faults!=faults:  return True
        else:                                           return False
    def return_probdens(self):
        """Gets the probability density associated with a Block and its components/actions (if any)"""
        state_pd = self.r.return_probdens()
        if hasattr(self, 'c'): 
            for compname, comp in self.c.components:
                state_pd*=comp.return_probdens()
        if hasattr(self, 'a'):
            for actionname, action in self.a.actions:
                state_pd*=action.return_probdens()
        return state_pd
    def create_hist(self, timerange, track='all'):
        """Initializes the function state history fxnhist of the model mdl over the time range timerange.
        A pointer to the history is then stored at self.h.
        
        Parameters
        ----------
        timerange : array
            Numpy array of times to initialize in the dictionary.
        track : 'all' or dict, 'none'), optional
            Which model states to track over time, which can be given as 'all' or a 
            dict of form {'functions':{'fxn1':'att1'}, 'flows':{'flow1':'att1'}}
            The default is 'all'.
        Returns
        -------
        fxnhist : dict
            A dictionary history of each recorded block property over the given timehist
        """
        if not hasattr(self, 'h'):
            if track:
                hist = History()
                for at in self.hist_atts:
                    at_track = get_sub_include(at, track)
                    attr = getattr(self, at, False)
                    if attr:
                        hist[at] = attr.create_hist(timerange, at_track)
                self.h=hist
            else: self.h=None
        return self.h
    def log_hist(self, t_ind):
        """
        Logs the history local to the block (and its attributes) by updating self.h

        Parameters
        ----------
        t_ind : int
            time-index of the history.
        """
        if hasattr(self, 'h'):
            self.h.log(self, t_ind)
            

## COMPONENT/COMPONENT ARCHITECTURES
class Component(Block):
    """
    Superclass for components (most attributes and methods inherited from Block superclass)
    """
    def __init__(self,name, **kwargs):
        """
        Inherit the component class

        Parameters
        ----------
        name : str
            Unique name ID for the component
        **kwargs: kwargs
            kwargs for the Block
        """
        super().__init__(name, **kwargs)
    def behavior(self,time):
        """ Placeholder for component behavior methods. Enables one to include components
        without yet having a defined behavior for them."""
        return 0
class CompArch(dataobject, mapping=True):
    """Container for holding component architectures"""
    archtype:       str = 'default'
    components:     dict = dict()
    faultmodes:     dict = dict()
    def make_components(self, CompClass, *args, **kwargs):
        """
        Adds components to the component architecture.

        Parameters
        ----------
        CompClass : Component
            Component to add
        *args : strs
            Names for the components to instantiate in the architecture
        **kwargs : kwargs
            keyword arguments to send CompClass, of form {'name':kwarg}.
            unless all have the same kwargs
        """
        if not self.components: self.components=dict()
        if not self.faultmodes: self.faultmodes=dict()
        
        for arg in args:
            if arg in kwargs:   kwargs_comp = kwargs[arg]
            else:               kwargs_comp = kwargs
            self.components[arg]=CompClass(arg, **kwargs_comp)
            self.faultmodes.update({self.components[arg].name+'_'+modename:arg 
                               for modename in self.components[arg].m.faultmodes})
    def copy_with_arg(self, **kwargs):
        cop = self.__class__(**kwargs)
        for compname, component in self.components.items(): #TODO: needs to cover all attributes, copy should a part of Block 
            cop_comp = cop.components[compname]
            cop_comp.s = cop_comp._init_s(**asdict(component.s))
            cop_comp.m.mirror(component.m)
            cop_comp.t = component.t.copy()
        return cop
    def inject_fault_in_component(self, fault):
        """
        Injects the fault fault in the component architecture.

        Parameters
        ----------
        fault : str
            Function-level name of the fault.
        """
        if fault in self.faultmodes:
            component = self.components[self.faultmodes[fault]]
            component.m.add_fault(fault[len(component.name):]+1)
    def update_seed(self, seed):
        for comp in self.components.values():
            comp.update_seed(seed)
    def get_rand_states(self, auto_update_only=False):
        rand_states={}
        for compname, comp in self.components.items():
            if comp.get_rand_states(auto_update_only=auto_update_only): 
                rand_states[compname] = comp.get_rand_states(auto_update_only=auto_update_only)
        return rand_states
    def get_faults(self):
        return {comp.name+'_'+f for comp in self.components.values() for f in comp.m.faults}
    def reset(self):
        for name, component in self.components.items():
            component.reset()
    def get_true_field(self, fieldname, *args, **kwargs):
        return get_true_field(self, fieldname, *args, **kwargs)
    def get_true_fields(self, *args, **kwargs):
        return get_true_fields(self, *args, **kwargs)
    def create_hist(self, timerange, track):
        """
        Creates a history corresponding to CompArch attributes.

        Parameters
        ----------
        timerange : iterable, optional
            Time-range to initialize the history over. The default is None.
        track : list/str/dict, optional
            argument specifying attributes for :func:`get_sub_include'. The default is None.

        Returns
        -------
        h : History
            History corresponding to the CompArch
        """
        h = History()
        for c, comp in self.components.items():
            comp_track = get_sub_include(c, track)
            if comp_track: 
                h[c]=comp.create_hist(timerange, comp_track)
        return h
    
## Actions/ASGs
class Action(Block):
    """
    Superclass for actions (most attributes and methods inherited from Block superclass)
    """
    def __init__(self,name, flows={}, **kwargs):
        """
        Inherit the Block class

        Parameters
        ----------
        name : str
            Unique name ID for the action
        flows : dict
            flows for the action
        **kwargs: kwargs
            kwargs for Block
        """
        self.name = name
        super().__init__(name, flows=flows,**kwargs)
    def updateact(self, time=0, run_stochastic=False, proptype='dynamic', dt=1.0):
        """
        Updates the behaviors, faults, times, etc of the action 

        Parameters
        ----------
        time : float, optional
            Model time. The default is 0.
        run_stochastic : bool
            Whether to run the simulation using stochastic or deterministic behavior
        """
        if time>self.t.time : self.r.update_stochastic_states()
        if proptype=='dynamic':
            if self.t.time<time:    self.behavior(time); self.t.t_loc+=dt
        else:                       self.behavior(time); self.t.t_loc+=dt
        self.t.time=time
    def behavior(self, time):
        """Placeholder behavior method for actions"""
        a=0
class ASG(dataobject, mapping=True):
    """
    Constructs the Action Sequence Graph with the given parameters.
    
    Parameters
    ----------
    initial_action : str/list
        Initial action to set as active. Default is 'auto'
            - 'auto' finds the starting node of the graph and uses it
            - 'ActionName' sets the given action as the first active action
            - providing a list of actions will set them all to active (if multi-state rep is used)
    state_rep : 'finite-state'/'multi-state'
        How the states of the system are represented. Default is 'finite-state'
            - 'finite-state' means only one action in the system can be active at once (i.e., a finite state machine)
            - 'multi-state' means multiple actions can be performed at once
    max_action_prop : 'until_false'/'manual'/int
        How actions progress. Default is 'until_false'
            - 'until_false' means actions are simulated until all outgoing conditions are false
            - providing an integer places a limit on the number of actions that can be performed per timestep
    proptype : 'static'/'dynamic'/'manual'
        Which propagation step to execute the Action Sequence Graph in. Default is 'dynamic'
            - 'manual' means that the propagation is performed manually (defined in a behavior method)
    per_timestep : bool
        Defines whether the action sequence graph is reset to the initial state each time-step (True) or stays in the current action (False). Default is False
    """
    actions:            dict = {}
    action_graph:       nx.DiGraph = nx.DiGraph()
    flow_graph:         nx.Graph = nx.Graph()
    conditions:         dict = {}
    faultmodes:         dict = {}
    flows:              dict = {}
    active_actions:     set = {}
    pos:                dict = {}
    initial_action="auto" 
    state_rep="finite-state" 
    max_action_prop="until_false" 
    proptype='dynamic' 
    per_timestep=False
    def __init__(self, *args, flows={}, **kwargs):
        super().__init__(*args, **kwargs)
        self.actions={} #TODO: remove restatement of defaults when fixed in recordclass
        self.action_graph= nx.DiGraph()
        self.flow_graph=nx.Graph()
        self.conditions={}
        self.faultmodes= {}
        self.flows = {}
        assoc_flows(self, flows=flows)
        self.active_actions = set()
    def build(self): #TODO: implement as post-__init__??
        if self.initial_action=='auto': 
            initial_action = [act for act, in_degree  in self.action_graph.in_degree if in_degree==0]
            if not initial_action: raise Exception("Cannot set initial action--no starting node")
        elif type(self.initial_action)==str: 
            initial_action=[self.initial_action]
        self.set_active_actions(initial_action)
        if self.state_rep=='finite-state' and len(self.active_actions)>1: 
            raise Exception("Cannot have more than one initial action with finite-state representation")
        if not self.pos: self.pos = nx.planar_layout(nx.compose(self.flow_graph, self.action_graph))
    def add_flow(self,flowname, fclass=Flow, p={}, s={}, flowtype=''):
        """
        Adds a flow with given attributes to ASG. Used to enable a flexible
        internal flow architecture in the ASG.

        Parameters
        ----------
        flowname : str
            Unique flow name to give the flow in the model
        fclass : Class, optional
            Class to instantiate (e.g. CommsFlow, MultiFlow). Default is Flow.
            Class must take flowname, flowdict, flowtype as input to __init__()
            May alternatively provide already-instanced object.
        p : dict, optional
            Parameter dictionary to instantiate the flow with
        s : dict, optional
            State dictionary to overwrite Flow default state values with
        flowtype : str, optional
            Denotes type for class (e.g. 'energy,' 'material,', 'signal')
        """
        if not getattr(self, 'is_copy', False):
            self.flows[flowname] = init_flow(flowname,fclass, p=p, s=s, flowtype=flowtype) 
    def add_act(self, name, actclass, *flownames, duration=0.0, **params):
        """
        Associate an Action with the Function Block for use in the Action Sequence Graph

        Parameters
        ----------
        name : str
            Internal Name for the Action
        action : Action
            Action class to instantiate
        *flows : flow
            Flows (optional) which connect the actions
        **params : any
            parameters to instantiate the Action with. 
        """
        flows = {fl:self.flows[fl] for fl in flownames}
        action = actclass(name, flows={**flows}, **params)
        self.actions[name] = action
        self.actions[name].duration=duration
        self.action_graph.add_node(name)
        self.flow_graph.add_node(name, bipartite=0)
        for flow in flows:
            self.flow_graph.add_node(flow, bipartite=1)
            self.flow_graph.add_edge(name,flow)
            
        modes_to_add = {action.name+'_'+f:val for f,val in action.m.faultmodes.items()}
        fmode_intersect = set(modes_to_add).intersection(self.faultmodes)
        if any(fmode_intersect):
            raise Exception("Action "+name+" overwrites existing fault modes: "+str(fmode_intersect)+". Rename the faults")
        self.faultmodes.update({action.name+'_'+modename:name for modename in action.m.faultmodes})
    def cond_pass(self):
        return True
    def add_cond(self, start_action, end_action, name='auto',condition='pass'):
        """
        Associates a Condition with the Function Block for use in the Action Sequence Graph

        Parameters
        ----------
        start_action : str
            Action where the condition is checked
        end_action : str
            Action that the condition leads to.
        name : str
            Name for the condition. Defaults to numbered conditions if none are provided.
        condition : method
            Method in the class to use as a condition. Defaults to self.condition_pass if none are provided
        """
        if name=='auto': name = str(len(self.conditions)+1)
        if condition=='pass': condition = self.cond_pass
        self.conditions[name] = condition
        self.action_graph.add_edge(start_action, end_action, **{'name':name, name:'name', 'arrow':True})
    def set_active_actions(self, actions):
        """Helper method for setting given action(s) as active"""
        if type(actions)==str: 
            if actions in self.actions: actions = [actions]
            else: raise Exception("initial_action="+actions+" not in self.actions: "+str(self.actions))
        if type(actions)==list:
            self.active_actions = set(actions)
            if any(self.active_actions.difference(self.actions)): raise Exception("Initial actions not associated with model: "+str(self.active_actions.difference(self.actions)))
        else: raise Exception("Invalid option for initial_action")
    def show(self, gtype='combined', with_cond_labels=True, pos=[]):
        """
        Shows a visual representation of the internal Action Sequence Graph of the Function Block

        Parameters
        ----------
        gtype : 'combined'/'flows'/'actions'
            Gives a graphical representation of the ASG. Default is 'combined'
            - 'actions'     (for function input):    plots the sequence of actions in the function's Action Sequence Graph
            - 'flows'       (for function input):    plots the action/flow connections in the function's Action Sequence Graph
            - 'combined'    (for function input):    plots both the sequence of actions in the functions ASG and action/flow connections
        with_cond_labels: Bool
            Whether or not to label the conditions
        pos : dict
            Dictionary of node positions for actions/flows
        """
        import matplotlib.pyplot as plt; plt.rcParams['pdf.fonttype'] = 42 
        fig = plt.figure()
        if gtype=='combined':       graph = nx.compose(self.flow_graph, self.action_graph)
        elif gtype=='flows':        graph = self.flow_graph
        elif gtype=='actions':      graph = self.action_graph
        if not pos and self.pos: pos = self.pos
        else:                    pos=nx.planar_layout(nx.compose(self.flow_graph, self.action_graph))
        nx.draw(graph, pos=pos, with_labels=True, node_color='grey')
        nx.draw_networkx_nodes(self.action_graph, pos=pos, node_shape='s', node_color='skyblue')
        nx.draw_networkx_nodes(self.action_graph, nodelist=self.active_actions, pos=pos, node_shape='s', node_color='green')
        edge_labels = {(in_node, out_node): label for in_node, out_node, label in graph.edges(data='name') if label}
        if with_cond_labels: nx.draw_networkx_edge_labels(graph, pos, edge_labels)
        if gtype=='combined' or gtype=='conditions':
            nx.draw_networkx_edges(self.action_graph, pos,arrows=True, arrowsize=30, arrowstyle='->', node_shape='s', node_size=100)
        return fig
    def prop_internal(self, time, run_stochastic, proptype, dt):
        """
        Propagates behaviors through the internal Action Sequence Graph

        Parameters
        ----------
        time : float, optional
            Model time. The default is 0.
        run_stochastic : bool/str
            Whether to run the simulation using stochastic or deterministic behavior
        proptype : str
            Type of propagation step to update ('behavior', 'static_behavior', or 'dynamic_behavior')
        """
        if not self.per_timestep: 
            self.set_active_actions(self.initial_action)
            for action in self.active_actions: self.actions[action].t.t_loc=0.0
        if proptype==self.proptype:
            active_actions = self.active_actions
            num_prop = 0
            while active_actions:
                new_active_actions=set(active_actions)
                for action in active_actions:
                    self.actions[action].updateact(time, run_stochastic, proptype=proptype, )
                    action_cond_edges = self.action_graph.out_edges(action, data=True)
                    for act_in, act_out, atts in action_cond_edges:
                        try:
                            cond = self.conditions[atts['name']]()
                        except TypeError as e:
                            raise TypeError("Poorly specified condition "+str(atts['name'])+": ") from e
                        if  cond and getattr(self.actions[action], 'duration',0.0)+dt<=self.actions[action].t.t_loc:
                            self.actions[action].t.t_loc=0.0
                            new_active_actions.add(act_out)
                            new_active_actions.discard(act_in)
                if len(new_active_actions)>1 and self.state_rep=='finite-state':
                    raise Exception("Multiple active actions in a finite-state representation: "+str(new_active_actions))
                num_prop +=1 
                if type(self.proptype)==int and num_prop>=self.proptype:
                    break
                if new_active_actions==set(active_actions):
                    break
                else: active_actions=new_active_actions
                if num_prop>10000: raise Exception("Undesired looping in Function ASG for: "+self.name)
            self.active_actions = active_actions
    def get_faults(self):
        return {act.name+f for act in self.actions.values() for f in act.m.faults}
    def update_seed(self, seed=[]):
        if seed:
            for act in self.actions.values():
                act.update_seed(seed)
    def copy(self, flows={}, **kwargs):
        new_flows = {**{fn:flow.copy() for fn, flow in self.flows.items() if fn not in flows}, **flows}
        
        cop = self.__init__(flows=new_flows, **kwargs)
        for action in self.actions: 
            cop.actions[action]=self.actions[action].copy()
        cop.active_actions = copy.deepcopy(self.active_actions)
        return cop
    def create_hist(self, timerange, track):
        """
        Creates a history corresponding to ASG attributes.

        Parameters
        ----------
        timerange : iterable, optional
            Time-range to initialize the history over. The default is None.
        track : list/str/dict, optional
            argument specifying attributes for :func:`get_sub_include'. The default is None.

        Returns
        -------
        h : History
            History corresponding to the ASG.
        """
        h = History()
        for c, comp in self.components.items():
            comp_track = get_sub_include(c, track)
            if comp_track: 
                h[c]=comp.create_hist(timerange, comp_track)
        h['active_actions'] = init_hist_iter('active_actions', self.active_actions, timerange=timerange, track='all')
        return h
#Function superclass 
class FxnBlock(Block):
    slots = ["c", "_c_arg", "a", "_a_arg"]
    hist_atts = ["c", "a"]+Block.hist_atts
    """
    Superclass class for functions which is a special type of Block\
    with c and a attributes for CompArch and ASGs, as well as a defined method for propagation
    """
    def __init__(self,name, flows={}, params={}, p=dict(), s=dict(), c=dict(), a=dict(), r=dict(), m=dict(), t=dict(),
                 local=dict(), comms=dict()):
        """
        Instantiates the function superclass with the relevant parameters.

        Parameters
        ----------
        flows :dict
            Flow objects passed from the model level to use instead of instantiating locally.
        p : dict, optional
            Internal parameters to override from defaults. The default is {}.
        s : dict, optional
            Internal states to override from defaults. The default is {}.
        c : dict, optional
            Internal CompArch fields/arguments override from defaults. The default is {}.
            FxnBlock must have an _init_c property.
        a : dict, optional
            Internal ASG fields/arguments override from defaults. The default is {}.
            FxnBlock must have an _init_a property.
        r : dict, optional
            Internal Rand fields/arguments override from defaults. The default is {}.
        r : dict, optional
            Internal Mode fields/arguments override from defaults. The default is {}.
        t : dict, optional
            Internal Time fields/arguments to override from defaults. The defautl is {}
        local : dict/list
            Views of MultiFlows to add instantiate local. May be of forms: 
                - {flowname:(localname, attrs)} (to only create local view of specific attributes)
                - {flowname:localname}          (to create view with all attributes)
                - [flowname1, flowname2...]     (to give overwrite the global flow with the local view of it)
        comms : dict/list
            Views of CommsFlows to add instantiate local. May be of forms: 
                - {flowname:(localname, attrs)} (to only create local view of specific attributes)
                - {flowname:localname}          (to create view with all attributes)
                - [flowname1, flowname2...]     (to give overwrite the global flow with the local view of it)
        """        
        super().__init__(name, flows=flows, p=p, s=s, r=r, m=m, t=t)
        
        for at in ['c', 'a']:
            at_arg = eval(at)
            at_init = getattr(self, '_init_'+at, False)
            if at_init:
                setattr(self, '_'+at+'_arg', at_arg)
                at_flows = dict()
                for flowname, flow in self.flows.items():
                    if hasattr(at_init, '_init_'+flowname): at_flows[flowname]=flow
                try:
                    if at_flows:    setattr(self, at,  at_init(flows=at_flows, **at_arg))
                    else:           setattr(self, at,  at_init(**at_arg))
                except TypeError as e:
                    invalid_args = [a for a in at_arg if a not in at_init.__fields__]
                    if invalid_args: argstr = ", Invalid args: "+', '.join(invalid_args)
                    else:            argstr=''
                    raise TypeError("Poor specification for : "+str(at_init)+" with kwargs: "+str(at_arg)+argstr) from e
                setattr(self, '_args_'+at,  at_arg)
                if at=='c':     compacts = self.c.components
                elif at=='a':   compacts = self.a.actions
                for ca in compacts.values():
                    self.m.faultmodes.update({ca.name+"_"+f:vals for f, vals in ca.m.faultmodes.items()})
            elif at_arg: 
                raise Exception(at+" argument provided: "+str(at_arg)+"without associating a CompArch/ASG to _init_"+at)
    def add_local_to_flowdict(self,flowdict, local, ftype):
        """
        Adds local flows to the flow dictionary during initialization

        Parameters
        ----------
        flowdict : dict
            Dictionary of flows {flowname:flow_object}
        local : dict/list
            Local flows to add. May be of forms: 
                - {flowname:(localname, attrs)} (to only create local view of specific attributes)
                - {flowname:localname}          (to create view with all attributes)
                - [flowname1, flowname2...]     (to give overwrite the global flow with the local view of it)
        ftype : str ('local'/'comms')
            Switches whether the flow added is to be a local or comms flow
        """
        for l in local:
            if ftype=='local':      gen_fl = flowdict[l].create_local
            elif ftype=='comms':    gen_fl = flowdict[l].create_comms
            if type(local)==dict and type(local[l]) in [list, tuple, set]:
                loc_flow = gen_fl(self.name, local[l][1])
                loc_name = local[l][0]
            elif type(local)==dict:
                loc_flow = gen_fl(self.name)
                loc_name = local[l]
            else:
                loc_flow = gen_fl(self.name)
                loc_name = l
            flowdict[loc_name]=loc_flow
    def copy(self, newflows, *args, **kwargs):
        """
        Creates a copy of the function object with newflows and arbitrary parameters associated with the copy. Used when copying the model.

        Parameters
        ----------
        newflows : list
            list of new flow objects to be associated with the copy of the function
        *attr : any
            arbitrary parameters to add (if funciton takes in more than flows e.g. design variables)

        Returns
        -------
        copy : FxnBlock
            Copy of the given function with new flows
        """
        cop = super().copy(newflows, *args, **kwargs)
        if hasattr(self, 'c'): cop.c = self.c.copy_with_arg(**self._c_arg)
        if hasattr(self, 'a'): cop.a = self.a.copy_with_arg(flows = cop.flows, **self._a_arg)
        return cop
    def updatefxn(self,proptype, faults=[], time=0, run_stochastic=False):
        """
        Updates the state of the function at a given time and injects faults.

        Parameters
        ----------
        proptype : str
            Type of propagation step to update ('behavior', 'static_behavior', or 'dynamic_behavior')
        faults : list, optional
            Faults to inject in the function. The default is [].
        time : float, optional
            Model time. The default is 0.
        run_stochastic : book
            Whether to run the simulation using stochastic or deterministic behavior
        """
        self.r.run_stochastic=run_stochastic
        if faults: self.m.add_fault(*faults)  #if there is a fault, it is instantiated in the function
        if hasattr(self, 'mode_state_dict') and any(faults): self.update_modestates()
        if hasattr(self, 'condfaults'):    self.condfaults(time)    #conditional faults and behavior are then run
        if time>self.t.time: self.r.update_stochastic_states()
        if hasattr(self, 'c'):    
            inject_faults_internal(self.c, faults)
        if hasattr(self, 'a'): 
            inject_faults_internal(self.a, faults)
            try:
                self.a.prop_internal(time, run_stochastic, proptype, self.t.dt)
            except TypeError as e:
                raise Exception("Poorly specified ASG: "+str(self.a.__class__)) from e
        
        if proptype=='static' and hasattr(self,'behavior'):        self.behavior(time)     #generic behavioral methods are run at all steps
        if proptype=='static' and hasattr(self,'static_behavior'): self.static_behavior(time)
        elif proptype=='dynamic' and hasattr(self,'dynamic_behavior') and time > self.t.time: 
            if self.t.run_times>=1:
                for i in range(self.t.run_times):
                    self.dynamic_behavior(time)
            elif not Decimal(str(time))%Decimal(str(self.t.dt)):
                self.dynamic_behavior(time)
        elif proptype=='reset':                                                             
            if hasattr(self,'static_behavior'):  self.static_behavior(time)
            if hasattr(self,'dynamic_behavior'): self.dynamic_behavior(time)
        
        actions = getattr(self, 'a', {'actions':{}})['actions']
        if actions:     # propagate faults from component level to function level
            self.m.faults.difference_update(self.a.faultmodes)
            self.m.faults.update(self.a.get_faults())
        comps = getattr(self, 'c', {'components':{}})['components']
        if comps:
            self.m.faults.difference_update(self.c.faultmodes)
            self.m.faults.update(self.c.get_faults())
        self.t.time=time
        if run_stochastic=='track_pdf': self.probdens = self.r.return_probdens()
        if (self.m.exclusive==True and len(self.m.faults)>1): 
            raise Exception("More than one fault present in "+self.name+"\n at t= "+str(time)+"\n faults: "+str(self.m.faults)+"\n Is the mode representation nonexclusive?")
        return
    def reset(self):
        super().reset()
        if hasattr(self, 'c'): self.c.reset()
        self.updatefxn('reset', faults=[], time=0)
class GenericFxn(FxnBlock):
    """Generic function block. For use when the user has not yet defined a class for the
    given (to be implemented) function block. Acts as a placeholder that enables simulation."""
    def __init__(self, name, flows):
        super().__init__(name, flows)




