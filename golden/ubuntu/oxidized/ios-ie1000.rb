class IE1000 < Oxidized::Model
  using Refinements

  prompt /^([\w.@()-]+[#>]\s?)$/
  comment '! '

  cmd :all do |cfg|
    cfg.gsub! /^% Invalid.*$/, ''
    cfg.cut_both
  end
  
  cmd 'show version' do |cfg|
  comments = []
    comments << "Product: #{Regexp.last_match(1)}" \
      if cfg =~ /^Product\s+:\s+(.*)$/
  
    comments << "Board: #{Regexp.last_match(1)}" \
      if cfg =~ /^Board Type\s+:\s+(.*)$/
  
    comments << "Software: #{Regexp.last_match(1)}" \
      if cfg =~ /^Software Version\s+:\s+(.*)$/
  
    comments << "Image: #{Regexp.last_match(1)}" \
      if cfg =~ /^Image\s+:\s+(.*)$/m
  
    comment comments.join("\n")
  end

  cmd 'show running-config' do |cfg|
    cfg = cfg.each_line.to_a[1..-1].join
    cfg
  end

  cmd :secret do |cfg|
    cfg.gsub!(/^(username .* password encrypted) .*/, '\1 <secret hidden>')
    cfg.gsub!(/^(cip security password encrypted) .*/, '\1 <secret hidden>')
    cfg.gsub!(/^(radius-server key encrypted) .*/,'\\1 <secret hidden>')
    cfg.gsub!(/^(tacacs-server key encrypted) .*/,'\\1 <secret hidden>')
    cfg
  end

  cfg :telnet do
    username /^Username:/i
    password /^Password:/i
  end

  cfg :telnet, :ssh do
    post_login 'terminal length 0'
    post_login 'terminal width 0'
    pre_logout 'exit'
  end
end