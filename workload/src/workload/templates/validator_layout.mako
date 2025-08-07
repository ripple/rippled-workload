# -*- coding: utf-8 -*-
<%inherit file="validator_layout.mako"/>

% for v in validators:
  <li>
    <span class="name">${v}</span>
    <span class="content">${post['content'] | n}</span>
  </li>
% endfor
