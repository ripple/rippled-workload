services:
% for v in validators:
${v}
% endfor
% for p in peers:
${p}
% endfor
% if use_fuzzer:
${fuzzer_service}
% endif
% if use_unl:
${unl_service}
% endif
networks:
  ${network_name}:
    name: ${network_name}
