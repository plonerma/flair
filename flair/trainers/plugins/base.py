import logging
from collections import defaultdict
from inspect import isclass
from itertools import count
from queue import Queue
from typing import (
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    NewType,
    Optional,
    Sequence,
    Set,
    Type,
    Union,
    cast,
)

log = logging.getLogger("flair")


PluginArgument = Union["BasePlugin", Type["BasePlugin"]]
HookHandleId = NewType("HookHandleId", int)

EventIdenifier = str


class TrainingInterrupt(Exception):
    """Allows plugins to interrupt the training loop."""


class Pluggable:
    """Dispatches events which attached plugins can react to."""

    valid_events: Optional[Set[EventIdenifier]] = None

    def __init__(self, *, plugins: Sequence[PluginArgument] = None):
        """Initialize a `Pluggable`.

        :param plugins: Plugins which should be attached to this `Pluggable`.
        """
        self._hook_handles: Dict[EventIdenifier, Dict[HookHandleId, HookHandle]] = defaultdict(dict)

        self._hook_handle_id_counter = count()

        self._plugins: List[BasePlugin] = []

        self._event_queue: Queue = Queue()
        self._processing_events = False

        if plugins is not None:
            for plugin in plugins:
                if isclass(plugin):
                    # instantiate plugin
                    plugin = plugin()

                plugin = cast("BasePlugin", plugin)
                plugin.attach_to(self)

    @property
    def plugins(self):
        return self._plugins

    def append_plugin(self, plugin):
        self._plugins.append(plugin)

    def validate_event(self, *events: EventIdenifier):
        for event in events:
            assert isinstance(event, EventIdenifier)

            if self.valid_events is not None:
                if event not in self.valid_events:
                    raise RuntimeError(f"Event '{event}' not recognized (available {self.valid_events})")
            return event

    def register_hook(self, func: Callable, *events: EventIdenifier):
        """Register a hook.

        :param func: Function to be called when the event is emitted.
        :param *events: List of events to call this function on.
        """

        self.validate_event(*events)

        handle: HookHandle = HookHandle(
            HookHandleId(next(self._hook_handle_id_counter)), events=events, func=func, pluggable=self
        )

        for event in events:
            self._hook_handles[event][handle.id] = handle
        return handle

    def dispatch(self, event: EventIdenifier, *args, **kwargs) -> dict:
        """Call all functions hooked to a certain event."""
        self.validate_event(event)

        events_return_value: dict = {}
        self._event_queue.put((event, args, kwargs, events_return_value))

        if not self._processing_events:
            self._processing_events = True

            while not self._event_queue.empty():
                event, args, kwargs, combined_return_values = self._event_queue.get()

                for hook in self._hook_handles[event].values():
                    returned = hook(*args, **kwargs)

                    if returned is not None:
                        combined_return_values.update(returned)

            self._processing_events = False

        # this dict may be empty and will be complete once all events have been
        # processed
        return events_return_value

    def remove_hook(self, handle: "HookHandle"):
        """Remove a hook handle from this instance."""
        for event in handle.events:
            del self._hook_handles[event][handle.id]


class HookHandle:
    """Represents the registration information of a hook callback."""

    def __init__(self, _id: HookHandleId, *, events: Sequence[EventIdenifier], func: Callable, pluggable: Pluggable):
        """Intitialize `HookHandle`.

        :param _id: Id, the callback is stored as in the `Pluggable`.
        :param *events: List of events, the callback is registered for.
        :param func: The callback function.
        :param pluggable: The `Pluggable` where the callback is registered.
        """
        pluggable.validate_event(*events)

        self._id = _id
        self._events = events
        self._func = func
        self._pluggable = pluggable

    @property
    def id(self) -> HookHandleId:
        """Return the id of this `HookHandle`."""
        return self._id

    @property
    def events(self) -> Iterator[EventIdenifier]:
        """Return iterator of events whis `HookHandle` is registered for."""
        yield from self._events

    def remove(self):
        """Remove a hook from the `Pluggable` it is attached to."""
        self._pluggable.remove_hook(self)

    def __call__(self, *args, **kw):
        """Call the hook this `HookHandle` is associated with."""
        self._func(*args, **kw)


class BasePlugin:
    """Base class for all plugins."""

    provided_events: Optional[Set[EventIdenifier]] = None

    dependencies: Iterable[Type["BasePlugin"]] = ()

    def __init__(self):
        """Initialize the base plugin."""
        self._hook_handles: List[HookHandle] = []
        self._pluggable: Optional[Pluggable] = None

    def attach_to(self, pluggable: Pluggable):
        """Attach this plugin to a `Pluggable`."""
        assert self._pluggable is None
        assert len(self._hook_handles) == 0

        self._pluggable = pluggable

        for dep in self.dependencies:
            dep_satisfied = False

            for plugin in pluggable.plugins:
                if isinstance(plugin, dep):
                    # there is already a plugin which satisfies this dependency
                    dep_satisfied = True
                    break

            if not dep_satisfied:
                # create a plugin of this type and attach it to the trainer
                dep_plugin = dep()
                dep_plugin.attach_to(pluggable)

        if self.provided_events is not None and pluggable.valid_events is not None:
            pluggable.valid_events = pluggable.valid_events | self.provided_events

        pluggable.append_plugin(self)

        # go through all attributes
        for name in dir(self):
            try:
                func = getattr(self, name)

                # get attribute hook events (mayr aise an AttributeError)
                events = getattr(func, "_plugin_hook_events")

                # register function as a hook
                handle = pluggable.register_hook(func, *events)
                self._hook_handles.append(handle)

            except AttributeError:
                continue

    def detach(self):
        """Detach a plugin from the `Pluggable` it is attached to."""
        assert self._pluggable is not None

        for handle in self._hook_handles:
            handle.remove()

        self._pluggable = None
        self._hook_handles = []

    @classmethod
    def mark_func_as_hook(cls, func: Callable, *events: EventIdenifier) -> Callable:
        """Mark method as a hook triggered by the `Pluggable`."""
        if len(events) == 0:
            events = (func.__name__,)
        setattr(func, "_plugin_hook_events", events)
        return func

    @classmethod
    def hook(
        cls,
        first_arg: Union[Callable, EventIdenifier] = None,
        *other_args: EventIdenifier,
    ) -> Callable:
        """Convience function for `BasePlugin.mark_func_as_hook`).

        Enables using the `@BasePlugin.hook` syntax.

        Can also be used as:
        `@BasePlugin.hook("some_event", "another_event")`
        """
        if first_arg is None:
            # Decorator was used with parentheses, but no args
            return cls.mark_func_as_hook

        if isinstance(first_arg, EventIdenifier):
            # Decorator was used with args (strings specifiying the events)
            def decorator_func(func: Callable):
                return cls.mark_func_as_hook(func, cast(EventIdenifier, first_arg), *other_args)

            return decorator_func

        # Decorator was used without args
        return cls.mark_func_as_hook(first_arg, *other_args)

    @property
    def pluggable(self) -> Optional[Pluggable]:
        return self._pluggable

    def __str__(self) -> str:
        return self.__class__.__name__


class TrainerPlugin(BasePlugin):
    @property
    def trainer(self):
        return self.pluggable

    @property
    def model(self):
        return self.trainer.model

    @property
    def corpus(self):
        return self.trainer.corpus
