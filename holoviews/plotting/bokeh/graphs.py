from __future__ import absolute_import, division, unicode_literals

from collections import defaultdict

import param
import numpy as np
from bokeh.models import (StaticLayoutProvider, NodesAndLinkedEdges,
                          EdgesAndLinkedNodes, Patches, Bezier, ColumnDataSource)

from ...core.data import Dataset
from ...core.util import (basestring, dimension_sanitizer, unique_array,
                          max_range)
from ...core.options import Cycle
from ...util.transform import dim
from ..util import process_cmap
from .chart import ColorbarPlot, PointPlot
from .element import CompositeElementPlot, LegendPlot
from .styles import line_properties, fill_properties, text_properties, rgba_tuple



class GraphPlot(CompositeElementPlot, ColorbarPlot, LegendPlot):

    selection_policy = param.ObjectSelector(default='nodes', objects=['edges', 'nodes', None], doc="""
        Determines policy for inspection of graph components, i.e. whether to highlight
        nodes or edges when selecting connected edges and nodes respectively.""")

    inspection_policy = param.ObjectSelector(default='nodes', objects=['edges', 'nodes', None], doc="""
        Determines policy for inspection of graph components, i.e. whether to highlight
        nodes or edges when hovering over connected edges and nodes respectively.""")

    tools = param.List(default=['hover', 'tap'], doc="""
        A list of plugin tools to use on the plot.""")

    # Deprecated options

    color_index = param.ClassSelector(default=None, class_=(basestring, int),
                                      allow_None=True, doc="""
        Deprecated in favor of color style mapping, e.g. `node_color=dim('color')`""")

    edge_color_index = param.ClassSelector(default=None, class_=(basestring, int),
                                      allow_None=True, doc="""
        Deprecated in favor of color style mapping, e.g. `edge_color=dim('color')`""")

    # Map each glyph to a style group
    _style_groups = {'scatter': 'node', 'multi_line': 'edge', 'patches': 'edge', 'bezier': 'edge'}

    style_opts = (['edge_'+p for p in fill_properties+line_properties] +
                  ['node_'+p for p in fill_properties+line_properties] +
                  ['node_size', 'cmap', 'edge_cmap', 'node_cmap'])

    _nonvectorized_styles =  ['cmap', 'edge_cmap', 'node_cmap']

    # Filled is only supported for subclasses
    filled = False

    # Bezier paths
    bezier = False

    # Declares which columns in the data refer to node indices
    _node_columns = [0, 1]

    @property
    def edge_glyph(self):
        if self.filled:
            return 'patches_1'
        elif self.bezier:
            return 'bezier_1'
        else:
            return 'multi_line_1'

    def _hover_opts(self, element):
        if self.inspection_policy == 'nodes':
            dims = element.nodes.dimensions()
            dims = [(dims[2].pprint_label, '@{index_hover}')]+dims[3:]
        elif self.inspection_policy == 'edges':
            kdims = [(kd.pprint_label, '@{%s_values}' % kd)
                     if kd in ('start', 'end') else kd for kd in element.kdims]
            dims = kdims+element.vdims
        else:
            dims = []
        return dims, {}

    def get_extents(self, element, ranges, range_type='combined'):
        return super(GraphPlot, self).get_extents(element.nodes, ranges, range_type)


    def _get_axis_labels(self, *args, **kwargs):
        """
        Override axis labels to group all key dimensions together.
        """
        element = self.current_frame
        xlabel, ylabel = [kd.pprint_label for kd in element.nodes.kdims[:2]]
        return xlabel, ylabel, None


    def _get_edge_colors(self, element, ranges, edge_data, edge_mapping, style):
        cdim = element.get_dimension(self.edge_color_index)
        if not cdim:
            return
        elstyle = self.lookup_options(element, 'style')
        cycle = elstyle.kwargs.get('edge_color')

        idx = element.get_dimension_index(cdim)
        field = dimension_sanitizer(cdim.name)
        cvals = element.dimension_values(cdim)
        if idx in self._node_columns:
            factors = element.nodes.dimension_values(2, expanded=False)
        elif idx == 2 and cvals.dtype.kind in 'uif':
            factors = None
        else:
            factors = unique_array(cvals)

        default_cmap = 'viridis' if factors is None else 'tab20'
        cmap = style.get('edge_cmap', style.get('cmap', default_cmap))
        nan_colors = {k: rgba_tuple(v) for k, v in self.clipping_colors.items()}
        if factors is None or (factors.dtype.kind in 'uif' and idx not in self._node_columns):
            colors, factors = None, None
        else:
            if factors.dtype.kind == 'f':
                cvals = cvals.astype(np.int32)
                factors = factors.astype(np.int32)
            if factors.dtype.kind not in 'SU':
                field += '_str__'
                cvals = [str(f) for f in cvals]
                factors = (str(f) for f in factors)
            factors = list(factors)
            if isinstance(cmap, dict):
                colors = [cmap.get(f, nan_colors.get('NaN', self._default_nan)) for f in factors]
            else:
                colors = process_cmap(cycle or cmap, len(factors))
        if field not in edge_data:
            edge_data[field] = cvals
        edge_style = dict(style, cmap=cmap)
        mapper = self._get_colormapper(cdim, element, ranges, edge_style,
                                       factors, colors, 'edge', 'edge_colormapper')
        transform = {'field': field, 'transform': mapper}
        color_type = 'fill_color' if self.filled else 'line_color'
        edge_mapping['edge_'+color_type] = transform
        edge_mapping['edge_nonselection_'+color_type] = transform
        edge_mapping['edge_selection_'+color_type] = transform


    def _get_edge_paths(self, element):
        path_data, mapping = {}, {}
        xidx, yidx = (1, 0) if self.invert_axes else (0, 1)
        if element._edgepaths is not None:
            edges = element._split_edgepaths.split(datatype='array', dimensions=element.edgepaths.kdims)
            if len(edges) == len(element):
                path_data['xs'] = [path[:, xidx] for path in edges]
                path_data['ys'] = [path[:, yidx] for path in edges]
                mapping = {'xs': 'xs', 'ys': 'ys'}
            else:
                raise ValueError("Edge paths do not match the number of supplied edges."
                                 "Expected %d, found %d paths." % (len(element), len(edges)))
        return path_data, mapping


    def get_data(self, element, ranges, style):
        # Force static source to False
        static = self.static_source
        self.handles['static_source'] = static
        self.static_source = False

        # Get node data
        nodes = element.nodes.dimension_values(2)
        node_positions = element.nodes.array([0, 1])
        # Map node indices to integers
        if nodes.dtype.kind not in 'uif':
            node_indices = {v: i for i, v in enumerate(nodes)}
            index = np.array([node_indices[n] for n in nodes], dtype=np.int32)
            layout = {str(node_indices[k]): (y, x) if self.invert_axes else (x, y)
                      for k, (x, y) in zip(nodes, node_positions)}
        else:
            index = nodes.astype(np.int32)
            layout = {str(k): (y, x) if self.invert_axes else (x, y)
                      for k, (x, y) in zip(index, node_positions)}
        point_data = {'index': index}
        cycle = self.lookup_options(element, 'style').kwargs.get('node_color')
        if isinstance(cycle, Cycle):
            style.pop('node_color', None)
            colors = cycle
        else:
            colors = None
        cdata, cmapping = self._get_color_data(
            element.nodes, ranges, style, name='node_fill_color',
            colors=colors, int_categories=True
        )
        point_data.update(cdata)
        point_mapping = cmapping
        if 'node_fill_color' in point_mapping:
            style = {k: v for k, v in style.items() if k not in
                     ['node_fill_color', 'node_nonselection_fill_color']}
            point_mapping['node_nonselection_fill_color'] = point_mapping['node_fill_color']

        edge_mapping = {}
        nan_node = index.max()+1 if len(index) else 0
        start, end = (element.dimension_values(i) for i in range(2))
        if nodes.dtype.kind == 'f':
            start, end = start.astype(np.int32), end.astype(np.int32)
        elif nodes.dtype.kind != 'i':
            start = np.array([node_indices.get(x, nan_node) for x in start], dtype=np.int32)
            end = np.array([node_indices.get(y, nan_node) for y in end], dtype=np.int32)
        path_data = dict(start=start, end=end)
        self._get_edge_colors(element, ranges, path_data, edge_mapping, style)
        if not static:
            pdata, pmapping = self._get_edge_paths(element)
            path_data.update(pdata)
            edge_mapping.update(pmapping)

        # Get hover data
        if 'hover' in self.handles:
            if self.inspection_policy == 'nodes':
                index_dim = element.nodes.get_dimension(2)
                point_data['index_hover'] = [index_dim.pprint_value(v) for v in element.nodes.dimension_values(2)]
                for d in element.nodes.dimensions()[3:]:
                    point_data[dimension_sanitizer(d.name)] = element.nodes.dimension_values(d)
            elif self.inspection_policy == 'edges':
                for d in element.dimensions():
                    dim_name = dimension_sanitizer(d.name)
                    if dim_name in ('start', 'end'):
                        dim_name += '_values'
                    path_data[dim_name] = element.dimension_values(d)
        data = {'scatter_1': point_data, self.edge_glyph: path_data, 'layout': layout}
        mapping = {'scatter_1': point_mapping, self.edge_glyph: edge_mapping}
        return data, mapping, style


    def _update_datasource(self, source, data):
        """
        Update datasource with data for a new frame.
        """
        if isinstance(source, ColumnDataSource):
            if self.handles['static_source']:
                source.trigger('data')
            else:
                source.data.update(data)
        else:
            source.graph_layout = data


    def _init_glyphs(self, plot, element, ranges, source):
        # Get data and initialize data source
        style = self.style[self.cyclic_index]
        data, mapping, style = self.get_data(element, ranges, style)
        edge_mapping = {k: v for k, v in mapping[self.edge_glyph].items()
                        if 'color' not in k}
        self.handles['previous_id'] = element._plot_id

        properties = {}
        mappings = {}
        for key in list(mapping):
            if not any(glyph in key for glyph in ('scatter_1', self.edge_glyph)):
                continue
            source = self._init_datasource(data.pop(key, {}))
            self.handles[key+'_source'] = source
            group_style = dict(style)
            style_group = self._style_groups.get('_'.join(key.split('_')[:-1]))
            others = [sg for sg in self._style_groups.values() if sg != style_group]
            glyph_props = self._glyph_properties(plot, element, source, ranges, group_style, style_group)
            for k, p in glyph_props.items():
                if any(k.startswith(o) for o in others):
                    continue
                properties[k] = p
            mappings.update(mapping.pop(key, {}))
        properties = {p: v for p, v in properties.items() if p not in ('legend', 'source')}
        properties.update(mappings)

        layout = data.pop('layout', {})
        if data and mapping:
            CompositeElementPlot._init_glyphs(self, plot, element, ranges, source,
                                              data, mapping, style)

        # Define static layout
        layout = StaticLayoutProvider(graph_layout=layout)
        node_source = self.handles['scatter_1_source']
        edge_source = self.handles[self.edge_glyph+'_source']
        renderer = plot.graph(node_source, edge_source, layout, **properties)

        # Initialize GraphRenderer
        if self.selection_policy == 'nodes':
            renderer.selection_policy = NodesAndLinkedEdges()
        elif self.selection_policy == 'edges':
            renderer.selection_policy = EdgesAndLinkedNodes()
        else:
            renderer.selection_policy = None

        if self.inspection_policy == 'nodes':
            renderer.inspection_policy = NodesAndLinkedEdges()
        elif self.inspection_policy == 'edges':
            renderer.inspection_policy = EdgesAndLinkedNodes()
        else:
            renderer.inspection_policy = None

        self.handles['layout_source'] = layout
        self.handles['glyph_renderer'] = renderer
        self.handles['scatter_1_glyph_renderer'] = renderer.node_renderer
        self.handles[self.edge_glyph+'_glyph_renderer'] = renderer.edge_renderer
        self.handles['scatter_1_glyph'] = renderer.node_renderer.glyph
        if self.filled or self.bezier:
            glyph_model = Patches if self.filled else Bezier
            allowed_properties = glyph_model.properties()
            for glyph_type in ('', 'selection_', 'nonselection_', 'hover_', 'muted_'):
                glyph = getattr(renderer.edge_renderer, glyph_type+'glyph', None)
                if glyph is None:
                    continue
                group_properties = dict(properties)
                props = self._process_properties(self.edge_glyph, group_properties, mappings)
                filtered = self._filter_properties(props, glyph_type, allowed_properties)
                new_glyph = glyph_model(**dict(filtered, **edge_mapping))
                setattr(renderer.edge_renderer, glyph_type+'glyph', new_glyph)
        self.handles[self.edge_glyph+'_glyph'] = renderer.edge_renderer.glyph
        if 'hover' in self.handles:
            if self.handles['hover'].renderers == 'auto':
                self.handles['hover'].renderers = []
            self.handles['hover'].renderers.append(renderer)



class ChordPlot(GraphPlot):

    labels = param.ClassSelector(class_=(basestring, dim), doc="""
        The dimension or dimension value transform used to draw labels from.""")

    show_frame = param.Boolean(default=False, doc="""
        Whether or not to show a complete frame around the plot.""")

    # Deprecated options

    label_index = param.ClassSelector(default=None, class_=(basestring, int),
                                      allow_None=True, doc="""
      Index of the dimension from which the node labels will be drawn""")
    
    # Map each glyph to a style group
    _style_groups = {'scatter': 'node', 'multi_line': 'edge', 'text': 'label',
                     'arc': 'arc'}

    style_opts = (GraphPlot.style_opts + ['label_'+p for p in text_properties])

    _draw_order = ['scatter', 'multi_line', 'layout']

    def get_extents(self, element, ranges, range_type='combined'):
        """
        A Chord plot is always drawn on a unit circle.
        """
        xdim, ydim = element.nodes.kdims[:2]
        if range_type not in ('combined', 'data', 'extents'):
            return xdim.range[0], ydim.range[0], xdim.range[1], ydim.range[1]
        no_labels = (element.nodes.get_dimension(self.label_index) is None and
                     self.labels is None)
        rng = 1.1 if no_labels else 1.4
        x0, x1 = max_range([xdim.range, (-rng, rng)])
        y0, y1 = max_range([ydim.range, (-rng, rng)])
        return (x0, y0, x1, y1)

    
    def _init_glyphs(self, plot, element, ranges, source):
        super(ChordPlot, self)._init_glyphs(plot, element, ranges, source)
        # Ensure that arc glyph matches node style
        if 'multi_line_2_glyph' in self.handles:
            scatter_props = self.handles['scatter_1_glyph'].properties_with_values()
            styles = {k.replace('fill', 'line'): v for k, v in scatter_props.items() if 'fill' in k}
            arc_renderer = self.handles['multi_line_2_glyph_renderer']
            scatter_renderer = self.handles['scatter_1_glyph_renderer']
            arc_renderer.data_source = scatter_renderer.data_source
            arc_renderer.view = scatter_renderer.view
            self.handles['multi_line_2_glyph'].update(**styles)
            self.handles['multi_line_2_source'] = scatter_renderer.data_source


    def get_data(self, element, ranges, style):
        offset = style.pop('label_offset', 1.05)
        data, mapping, style = super(ChordPlot, self).get_data(element, ranges, style)
        angles = element._angles
        arcs = defaultdict(list)
        for i in range(len(element.nodes)):
            start, end = angles[i:i+2]
            vals = np.linspace(start, end, 20)
            xs, ys = np.cos(vals), np.sin(vals)
            arcs['arc_xs'].append(xs)
            arcs['arc_ys'].append(ys)
        data['scatter_1'].update(arcs)
        data['multi_line_2'] = data['scatter_1']
        mapping['multi_line_2'] = {'xs': 'arc_xs', 'ys': 'arc_ys', 'line_width': 10}

        label_dim = element.nodes.get_dimension(self.label_index)
        labels = self.labels
        if label_dim and labels:
            self.warning("Cannot declare style mapping for 'labels' option "
                         "and declare a label_index; ignoring the label_index.")
        elif label_dim:
            labels = label_dim
        elif isinstance(labels, basestring):
            labels = element.nodes.get_dimension(labels)

        if labels is None:
            return data, mapping, style

        nodes = element.nodes
        if element.vdims:
            values = element.dimension_values(element.vdims[0])
            if values.dtype.kind in 'uif':
                edges = Dataset(element)[values>0]
                nodes = list(np.unique([edges.dimension_values(i) for i in range(2)]))
                nodes = element.nodes.select(**{element.nodes.kdims[2].name: nodes})
        xs, ys = (nodes.dimension_values(i)*offset for i in range(2))
        if isinstance(labels, dim):
            text = labels.apply(element, flat=True)
        else:
            text = element.nodes.dimension_values(labels)
            text = [labels.pprint_value(v) for v in text]
        angles = np.arctan2(ys, xs)
        data['text_1'] = dict(x=xs, y=ys, text=[str(l) for l in text], angle=angles)
        mapping['text_1'] = dict(text='text', x='x', y='y', angle='angle', text_baseline='middle')
        return data, mapping, style



class NodePlot(PointPlot):
    """
    Simple subclass of PointPlot which hides x, y position on hover.
    """

    def _hover_opts(self, element):
        return element.dimensions()[2:], {}



class TriMeshPlot(GraphPlot):

    filled = param.Boolean(default=False, doc="""
        Whether the triangles should be drawn as filled.""")

    style_opts = (['edge_'+p for p in line_properties+fill_properties] +
                  ['node_'+p for p in fill_properties+line_properties] +
                  ['node_size', 'cmap', 'edge_cmap', 'node_cmap'])

    # Declares that three columns in TriMesh refer to edges
    _node_columns = [0, 1, 2]

    def _process_vertices(self, element):
        style = self.style[self.cyclic_index]
        edge_color = style.get('edge_color')
        if edge_color not in element.nodes:
            edge_color = self.edge_color_index
        simplex_dim = element.get_dimension(edge_color)
        vertex_dim = element.nodes.get_dimension(edge_color)
        if vertex_dim and not simplex_dim:
            simplices = element.array([0, 1, 2])
            z = element.nodes.dimension_values(vertex_dim)
            z = z[simplices].mean(axis=1)
            element = element.add_dimension(vertex_dim, len(element.vdims), z, vdim=True)
        element.edgepaths
        return element

    def _init_glyphs(self, plot, element, ranges, source):
        element = self._process_vertices(element)
        super(TriMeshPlot, self)._init_glyphs(plot, element, ranges, source)

    def _update_glyphs(self, element, ranges):
        element = self._process_vertices(element)
        super(TriMeshPlot, self)._update_glyphs(element, ranges)
