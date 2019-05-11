# -*- coding: utf-8 -*-

"""Qt dock window."""


# -----------------------------------------------------------------------------
# Imports
# -----------------------------------------------------------------------------

from collections import defaultdict
from functools import partial
import json
import logging
import os.path as op

from .qt import (QApplication, QWidget, QDockWidget, QStatusBar, QMainWindow,
                 QMessageBox, Qt, QSize, QMetaObject, _wait)
from .actions import Actions, Snippets
from phylib.utils import Bunch, _bunchify, emit, connect, _load_json, _save_json
from phy.utils import _ensure_dir_exists, phy_config_dir

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# GUI main window
# -----------------------------------------------------------------------------

def _try_get_matplotlib_canvas(view):
    # Get the Qt widget from a matplotlib figure.
    try:
        from matplotlib.pyplot import Figure
        from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg
        if isinstance(view, Figure):
            view = FigureCanvasQTAgg(view)
        # Case where the view has a .figure property which is a matplotlib figure.
        elif isinstance(getattr(view, 'figure', None), Figure):
            view = FigureCanvasQTAgg(view.figure)
    except ImportError as e:  # pragma: no cover
        logger.warning("Import error: %s", e)
    return view


def _try_get_opengl_canvas(view):
    """Convert from QOpenGLWindow to QOpenGLWidget."""
    from phy.plot.base import BaseCanvas
    if isinstance(view, BaseCanvas):
        return QWidget.createWindowContainer(view)
    elif isinstance(getattr(view, 'canvas', None), BaseCanvas):
        return QWidget.createWindowContainer(view.canvas)
    return view


class DockWidget(QDockWidget):
    def closeEvent(self, e):
        """Qt slot when the window is closed."""
        emit('close_dock_widget', self)
        super(DockWidget, self).closeEvent(e)


def _create_dock_widget(widget, name, closable=True, floatable=True):
    # Create the gui widget.
    dock_widget = DockWidget()
    dock_widget.setObjectName(name)
    dock_widget.setWindowTitle(name)
    dock_widget.setWidget(widget)

    # Set gui widget options.
    options = QDockWidget.DockWidgetMovable
    if closable:
        options = options | QDockWidget.DockWidgetClosable
    if floatable:
        options = options | QDockWidget.DockWidgetFloatable

    dock_widget.setFeatures(options)
    dock_widget.setAllowedAreas(Qt.LeftDockWidgetArea |
                                Qt.RightDockWidgetArea |
                                Qt.TopDockWidgetArea |
                                Qt.BottomDockWidgetArea
                                )

    return dock_widget


def _get_dock_position(position):
    return {'left': Qt.LeftDockWidgetArea,
            'right': Qt.RightDockWidgetArea,
            'top': Qt.TopDockWidgetArea,
            'bottom': Qt.BottomDockWidgetArea,
            }[position or 'right']


class GUI(QMainWindow):
    """A Qt main window holding docking Qt.

    `GUI` derives from `QMainWindow`.

    Events
    ------

    close
    show
    add_view
    close_view

    """
    def __init__(self,
                 position=None,
                 size=None,
                 name=None,
                 subtitle=None,
                 view_creator=None,
                 **kwargs
                 ):
        # HACK to ensure that closeEvent is called only twice (seems like a
        # Qt bug).
        self._closed = False
        if not QApplication.instance():  # pragma: no cover
            raise RuntimeError("A Qt application must be created.")
        super(GUI, self).__init__()
        QMetaObject.connectSlotsByName(self)
        self.setDockOptions(QMainWindow.AllowTabbedDocks |
                            QMainWindow.AllowNestedDocks |
                            QMainWindow.AnimatedDocks
                            )

        self._set_name(name, subtitle)
        position = position or (200, 200)
        size = size or (800, 600)
        self._set_pos_size(position, size)

        # Registered functions.
        self._registered = {}

        # Mapping {name: menuBar}.
        self._menus = {}

        # Views,
        self._views = []
        self._view_class_indices = defaultdict(int)  # Dictionary {view_cls: next_usable_index}

        # View creator: dictionary {view_class: function_that_adds_view}
        self.view_creator = view_creator or {}

        # Status bar.
        self._lock_status = False
        self._status_bar = QStatusBar(self)
        self.setStatusBar(self._status_bar)

        # List of attached Actions instances.
        self.actions = []

        # Default actions.
        self._set_default_actions()

        # Create and attach snippets.
        self.snippets = Snippets(self)

        # Create the state.
        self.state = GUIState(self.name, **kwargs)

        @connect(sender=self)
        def on_show(sender):
            logger.debug("Load the geometry state.")
            gs = self.state.get('geometry_state', None)
            self.restore_geometry_state(gs)

    def _set_name(self, name, subtitle):
        if name is None:
            name = self.__class__.__name__
        title = name if not subtitle else name + ' - ' + subtitle
        self.setWindowTitle(title)
        self.setObjectName(name)
        # Set the name in the GUI.
        self.name = name

    def _set_pos_size(self, position, size):
        if position is not None:
            self.move(position[0], position[1])
        if size is not None:
            self.resize(QSize(size[0], size[1]))

    def _set_default_actions(self):
        self.default_actions = Actions(self, name='Default', menu='&File')

        @self.default_actions.add(shortcut=('HelpContents', 'h'))
        def show_all_shortcuts():
            """Show the shortcuts of all actions."""
            for actions in self.actions:
                actions.show_shortcuts()

        @self.default_actions.add(shortcut='?')
        def about():  # pragma: no cover
            """Display an about dialog."""
            from phy import __version_git__
            msg = "phy {} v{}".format(self.name, __version_git__)
            try:
                from phycontrib import __version__
                msg += "\nphycontrib v{}".format(__version__)
            except ImportError:
                pass
            QMessageBox.about(self, "About", msg)

        @self.default_actions.add(shortcut='ctrl+q')
        def exit():
            """Close the GUI."""
            self.close()

        self.default_actions.separator()

    # Events
    # -------------------------------------------------------------------------

    def closeEvent(self, e):
        """Qt slot when the window is closed."""
        if self._closed:
            return
        _wait(250)
        res = emit('close', self)
        # Discard the close event if False is returned by one of the callback
        # functions.
        if False in res:  # pragma: no cover
            e.ignore()
            return
        super(GUI, self).closeEvent(e)
        self._closed = True

        # Save the state to disk when closing the GUI.
        logger.debug("Save the geometry state.")
        gs = self.save_geometry_state()
        self.state['geometry_state'] = gs
        self.state.save()

    def show(self):
        """Show the window."""
        emit('show', self)
        super(GUI, self).show()

    # Views
    # -------------------------------------------------------------------------

    @property
    def views(self):
        return self._views

    @property
    def view_count(self):
        """Dictionary {view_class: n_views}."""
        vc = defaultdict(int)
        for v in self.views:
            vc[v.__class__] += 1
        return dict(vc)

    def list_views(self, cls):
        """Return the list of views from a given class."""
        return [view for view in self._views if view.__class__ == cls]

    def get_view(self, cls, index=0):
        """Return a view from a given class."""
        views = self.list_views(cls)
        if index <= len(views) - 1:
            return views[index]

    def _set_view_name(self, view):
        """Set a unique name for a view: view class name, followed by the view index."""
        assert view not in self._views
        # Get all views of the same class.
        cls = view.__class__
        basename = cls.__name__
        views = self.list_views(view.__class__)
        if not views:
            # If the view is the first of its class, just use the base name.
            name = basename
        else:
            # index is the next usable index for the view's class.
            index = self._view_class_indices.get(cls, 0)
            assert index >= 1
            name = '%s (%d)' % (basename, index)
        view.name = name
        return name

    def _create_and_add_view(self, view_cls):
        fn = self.view_creator.get(view_cls, None)
        if fn is None:
            return
        # Create the view with the view creation function.
        view = fn()
        # Attach the view to the GUI if it has an attach(gui) method,
        # otherwise add the view.
        if hasattr(view, 'attach'):
            view.attach(self)
        else:
            self.add_view(view)
        return view

    def create_views(self, view_count):
        """view_count is a dictionary {view_cls: n_views}."""
        for view_cls, n_views in view_count.items():
            if n_views <= 0:
                continue
            assert n_views >= 1
            # Add "Add view" action.
            first_view = self._create_and_add_view(view_cls)
            actions = getattr(first_view, 'actions', None)
            if not actions:
                continue
            actions.separator()
            actions.add(
                partial(self._create_and_add_view, view_cls), name='add_%s' % view_cls.__name__)
            # Extra views.
            for i in range(n_views - 1):
                self._create_and_add_view(view_cls)

    def add_view(self, view, position=None, closable=True, floatable=True, floating=None):
        """Add a widget to the main window."""

        name = self._set_view_name(view)
        self._views.append(view)
        self._view_class_indices[view.__class__] += 1

        # Get the Qt canvas for matplotlib/OpenGL views.
        widget = _try_get_matplotlib_canvas(view)
        widget = _try_get_opengl_canvas(widget)

        dock_widget = _create_dock_widget(widget, name,
                                          closable=closable,
                                          floatable=floatable,
                                          )
        self.addDockWidget(_get_dock_position(position), dock_widget, Qt.Horizontal)
        if floating is not None:
            dock_widget.setFloating(floating)
        dock_widget.view = view
        view.dock_widget = dock_widget

        # Emit the close_view event when the dock widget is closed.
        @connect(sender=dock_widget)
        def on_close_dock_widget(sender):
            self._views.remove(view)
            emit('close_view', self, view)

        dock_widget.show()
        emit('add_view', self, view)
        logger.log(5, "Add %s to GUI.", name)
        return dock_widget

    # Menu bar
    # -------------------------------------------------------------------------

    def get_menu(self, name):
        """Return or create a menu."""
        if name not in self._menus:
            self._menus[name] = self.menuBar().addMenu(name)
        return self._menus[name]

    def dialog(self, message):
        """Show a message in a dialog box."""
        box = QMessageBox(self)
        box.setText(message)
        return box

    # Status bar
    # -------------------------------------------------------------------------

    @property
    def status_message(self):
        """The message in the status bar."""
        return str(self._status_bar.currentMessage())

    @status_message.setter
    def status_message(self, value):
        if self._lock_status:
            return
        self._status_bar.showMessage(str(value))

    def lock_status(self):
        self._lock_status = True

    def unlock_status(self):
        self._lock_status = False

    # State
    # -------------------------------------------------------------------------

    def save_geometry_state(self):
        """Return picklable geometry and state of the window and docks.

        This function can be called in `on_close()`.

        """
        return {
            'geometry': self.saveGeometry(),
            'state': self.saveState(),
        }

    def restore_geometry_state(self, gs):
        """Restore the position of the main window and the docks.

        The gui widgets need to be recreated first.

        This function can be called in `on_show()`.

        """
        if not gs:
            return
        if gs.get('geometry', None):
            self.restoreGeometry((gs['geometry']))
        if gs.get('state', None):
            self.restoreState((gs['state']))


# -----------------------------------------------------------------------------
# GUI state, creator
# -----------------------------------------------------------------------------

class GUIState(Bunch):
    """Represent the state of the GUI: positions of the views and
    all parameters associated to the GUI and views.

    This is automatically loaded from the configuration directory.

    """
    def __init__(self, name='GUI', config_dir=None, **kwargs):
        super(GUIState, self).__init__(**kwargs)
        self.name = name
        self.config_dir = config_dir or phy_config_dir()
        _ensure_dir_exists(op.join(self.config_dir, self.name))
        self.load()

    def get_view_state(self, view, gui):
        """Return the state of a view."""
        return self.get(view.name, Bunch())

    def update_view_state(self, view, state, gui):
        """Update the state of a view."""
        name = view.name
        if name not in self:
            self[name] = Bunch()
        self[name].update(state)
        logger.debug("Update GUI state for %s", name)

    @property
    def path(self):
        return op.join(self.config_dir, self.name, 'state.json')

    def load(self):
        """Load the state from the JSON file in the config dir."""
        if not op.exists(self.path):
            logger.debug("The GUI state file `%s` doesn't exist.", self.path)
            # TODO: create the default state.
            return
        assert op.exists(self.path)
        logger.debug("Load the GUI state from `%s`.", self.path)
        try:
            data = _load_json(self.path)
        except json.decoder.JSONDecodeError as e:  # pragma: no cover
            logger.warning("Error decoding JSON: %s", e)
            data = {}
        self.update(_bunchify(data))

    def save(self):
        """Save the state to the JSON file in the config dir."""
        logger.debug("Save the GUI state to `%s`.", self.path)
        data = {k: v for k, v in self.items() if k not in ('config_dir', 'name')}
        _save_json(self.path, data)
