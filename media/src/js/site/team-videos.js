/* Amara, universalsubtitles.org
 *
 * Copyright (C) 2015 Participatory Culture Foundation
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Affero General Public License as
 * published by the Free Software Foundation, either version 3 of the
 * License, or (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU Affero General Public License for more details.
 *
 * You should have received a copy of the GNU Affero General Public License
 * along with this program.  If not, see
 * http://www.gnu.org/licenses/agpl-3.0.html.
 */
(function() {

$(document).ready(function() {
    $('html#team-videos .bottom-sheet button[data-modal-url]').click(onBottomButtonClick);
    $('html#team-videos button.open-edit-modal').click(onEditModalClick);
});

function onBottomButtonClick(evt) {
    var selection = [];
    $('input[name=team_videos]:checked').each(function() {
        selection.push($(this).val());
    });
    onAjaxOpenModalClick(evt, selection, $(this));
}

function onEditModalClick(evt) {
    var button = $(this);
    onAjaxOpenModalClick(evt, [button.data('videoId')], button);
}

function onAjaxOpenModalClick(evt, selection, button) {
    var url = button.data('modalUrl');
    var params = getQueryParams();

    evt.preventDefault();
    params.selection = selection.join('-');
    ajaxOpenModal(url, params);
}


})();

