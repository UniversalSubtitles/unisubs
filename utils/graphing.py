# -*- coding: utf-8 -*-
# Amara, universalsubtitles.org
#
# Copyright (C) 2014 Participatory Culture Foundation
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see
# http://www.gnu.org/licenses/agpl-3.0.html.

import math
import pygal
from pygal.style import Style
import os, settings

custom_css = '''
{{ id }}.tooltip rect {
  fill: gray !important;
}
{{ id }}.tooltip text {
  font-size: 13px !important;
}
'''

custom_css_file = os.path.join(settings.TMP_FOLDER, 'pygal_custom_style.css')

with open(custom_css_file, 'w') as f:
  f.write(custom_css)

def plot(data, title=None, graph_type='Pie', max_entries=None, other_label="Other", y_title=None, labels=False, xlinks=False, total_label="Edited: "):
    custom_style = Style(
        background='transparent',
        font_family='sans-serif',
        plot_background='transparent',
        foreground='FFFFFF',
        foreground_light='black',
        foreground_dark='#FFFFFF',
        opacity='.9',
        opacity_hover='0.5',
        transition='400ms ease-in',
        colors=('#5da5da', '#faa43a', '#60bd68', '#f17cb0', '#b2912f', '#b276b2', '#decf3f', '#f15854', '#4d4d4d'))
    data.sort(reverse=True, key=lambda x:x[1])
    if graph_type == 'Pie':
        chart = pygal.Pie(style=custom_style, inner_radius=.4)
    else:
        config = pygal.Config(style=custom_style, legend_at_bottom=True, explicit_size=True, legend_font_size=12)
        config.css.append(custom_css_file)
        chart = pygal.Bar(config)
        if data:
          if (len(data) > 1) and (data[0][1] > 100) and (data[0][1] > 3*data[1][1]):
            maximum = 2 * data[1][1]
            chart.y_labels = range(0, maximum, max(1,int(round(maximum/10, -int(math.floor(math.log10(1+ maximum/10)))))))
            chart.value_formatter = lambda x: str(int(x))
            chart.range = (0, maximum)
          else:
            maximum = data[0][1] + 1
            chart.y_labels = map(repr, range(0, maximum, max(1,int(round(maximum/10, -int(math.floor(math.log10(1+ maximum/10))))))))
            chart.y_labels = range(0, maximum, max(1,int(round(maximum/10, -int(math.floor(math.log10(1+ maximum/10)))))))
            chart.value_formatter = lambda x: str(int(x))
    if title:
        chart.title = title
    data.sort(reverse=True, key=lambda x:x[1])
    if max_entries and (len(data) > max_entries):
        remaining_data = reduce(lambda x, y: ('',x[1]+y[1]), data[max_entries:])
        data = data[:max_entries]
    for item in data:
        label = ''
        if labels:
            label = item[2]
        else:
            label = item[0]
        if xlinks:
          chart.add(item[0], [{'value': item[1],
                               'label': label,
                               'value 2': total_label,
                               'xlink': {
                                 'href': item[3],
                                 'target': '_blank'}}])
        else:
          chart.add(item[0], [{'value': item[1], 'label': label, 'value 2': total_label}])
    if y_title:
        chart.y_title = y_title
    if len(data) < 4:
        chart.width = 450
        chart.height = 345
    else:
        chart.width = 645
        chart.height = 517
    return chart.render()
